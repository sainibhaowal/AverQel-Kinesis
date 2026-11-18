"""Tests for DeepSpace Jobs Registry."""

from __future__ import annotations

import asyncio

import pytest

from app.deepspace.services.jobs.brand import job_channel, job_kind
from app.deepspace.services.jobs.local_registry import LocalJobRegistry
from app.deepspace.services.jobs.types import (
    JobHandle,
    JobHooks,
    JobOutcome,
    JobSpec,
)


class TestJobSpec:
    def test_job_spec_creation(self) -> None:
        spec = JobSpec(
            kind=job_kind("bash"),
            label="echo hello",
            output_limit_bytes=1024,
        )
        assert str(spec.kind) == "bash"
        assert spec.label == "echo hello"
        assert spec.output_limit_bytes == 1024


class TestLocalJobRegistry:
    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        import asyncio

        self._event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._event_loop)

        # Create a mock context with agents registry
        mock_agents = type(
            "MockAgents",
            (),
            {
                "get": lambda self, session_id: None,
                "list": lambda self: [],
            },
        )()

        mock_ctx = type(
            "MockContext",
            (),
            {
                "get": lambda self, name: mock_agents if name == "agents" else None,
                "logger": type(
                    "MockLogger",
                    (),
                    {
                        "warning": lambda self, msg: None,
                    },
                )(),
                "events": type(
                    "MockEvents",
                    (),
                    {
                        "dispatch": lambda self, *args, **kwargs: [],
                    },
                )(),
                "effect": lambda self, fn, label: (lambda: None),  # Mock effect returns disposer
            },
        )()

        self.registry = LocalJobRegistry(ctx=mock_ctx, config={})

        yield

        self._event_loop.close()

    def _make_spec(self) -> JobSpec:

        done_future: asyncio.Future = self._event_loop.create_future()
        done_future.set_result(JobOutcome(status="completed"))

        class MockSpec(JobSpec):
            def run(self, job: JobHandle) -> JobHooks:
                return JobHooks(
                    cancel=lambda reason: None,
                    done=done_future,
                )

        return MockSpec(kind=job_kind("bash"), label="test")

    @pytest.mark.asyncio
    async def test_start_requires_controller(self) -> None:
        # Test that owned jobs require a controller
        import asyncio

        _ = type(
            "MockAgents",
            (),
            {
                "get": lambda self, session_id: None,
                "list": lambda self: [],
            },
        )()

        # Create a mock agent with a session
        mock_session = type(
            "MockSession",
            (),
            {
                "id": "test-session",
            },
        )()

        mock_agent = type(
            "MockAgent",
            (),
            {
                "id": "test-agent",
                "session": mock_session,
                "ctx": type(
                    "MockCtx",
                    (),
                    {
                        "scope_key": "test-scope",
                        "effect": lambda self, fn, label: (lambda: None),
                    },
                )(),
            },
        )()

        mock_agents_with_agent = type(
            "MockAgents",
            (),
            {
                "get": lambda self, session_id: (
                    mock_agent if session_id == "test-session" else None
                ),
                "list": lambda self: [mock_agent],
            },
        )()

        mock_ctx = type(
            "MockContext",
            (),
            {
                "get": lambda self, name: mock_agents_with_agent if name == "agents" else None,
                "logger": type(
                    "MockLogger",
                    (),
                    {
                        "warning": lambda self, msg: None,
                    },
                )(),
                "events": type(
                    "MockEvents",
                    (),
                    {
                        "dispatch": lambda self, *args, **kwargs: [],
                    },
                )(),
                "effect": lambda self, fn, label: (lambda: None),
            },
        )()

        registry = LocalJobRegistry(ctx=mock_ctx, config={})
        # Create a spec with owner
        from app.deepspace.services.jobs.brand import job_kind

        done_future: asyncio.Future = asyncio.get_event_loop().create_future()
        done_future.set_result(JobOutcome(status="completed"))

        class MockSpecWithOwner(JobSpec):
            def run(self, job: JobHandle) -> JobHooks:
                return JobHooks(
                    cancel=lambda reason: None,
                    done=done_future,
                )

        spec = MockSpecWithOwner(kind=job_kind("bash"), label="test", owner="test-session")
        with pytest.raises(RuntimeError, match="no job controller serves this agent"):
            registry.start(spec)

    @pytest.mark.asyncio
    async def test_attach_controller_enables_start(self) -> None:
        self.registry.attach_controller("test")
        spec = self._make_spec()
        job_id = self.registry.start(spec)
        assert hasattr(job_id, "_brand_name") and job_id._brand_name == "JobId"
        assert str(job_id).startswith("bash-")

    @pytest.mark.asyncio
    async def test_list_returns_jobs(self) -> None:
        self.registry.attach_controller("test")
        spec1 = self._make_spec()
        self.registry.start(spec1)

        done_future2: asyncio.Future = self._event_loop.create_future()
        done_future2.set_result(JobOutcome(status="completed"))

        class MockSpec2(JobSpec):
            def run(self, job: JobHandle) -> JobHooks:
                return JobHooks(
                    cancel=lambda reason: None,
                    done=done_future2,
                )

        spec2 = MockSpec2(kind=job_kind("bash"), label="test1")
        self.registry.start(spec2)
        jobs = self.registry.list()
        assert len(jobs) == 2

    @pytest.mark.asyncio
    async def test_get_job(self) -> None:
        self.registry.attach_controller("test")
        spec = self._make_spec()
        job_id = self.registry.start(spec)
        job = self.registry.get(job_id)
        assert job.id == job_id

    @pytest.mark.asyncio
    async def test_kill_job(self) -> None:
        self.registry.attach_controller("test")
        spec = self._make_spec()
        job_id = self.registry.start(spec)
        result = self.registry.kill(job_id)
        assert result == "requested"

    @pytest.mark.asyncio
    async def test_read_job(self) -> None:
        self.registry.attach_controller("test")
        spec = self._make_spec()
        job_id = self.registry.start(spec)
        # The job runs asynchronously, so we just check the read doesn't error
        read = self.registry.read(job_id)
        assert read.job.id == job_id


class TestOutputRing:
    def test_append_and_read(self) -> None:
        from app.deepspace.services.jobs.ring import OutputRing

        ring = OutputRing(retain_bytes=1024)
        ring.append("hello", channel=job_channel("stdout"))
        chunks, lossy = ring.read_from_model_cursor()
        assert len(chunks) == 1
        assert chunks[0].text == "hello"

    def test_retention_trim(self) -> None:
        from app.deepspace.services.jobs.ring import OutputRing

        # Ring keeps whole chunks, so a single 100-byte chunk won't be split
        # It will be retained in full since it's the only chunk
        ring = OutputRing(retain_bytes=50)
        ring.append("x" * 100)
        assert ring.total == 100
        assert ring.earliest == 0  # Whole chunk retained
        # Multiple chunks: oldest ones dropped first
        ring2 = OutputRing(retain_bytes=50)
        ring2.append("a" * 30)
        ring2.append("b" * 30)
        ring2.append("c" * 30)
        assert ring2.total == 90
        assert ring2.earliest == 30  # First chunk (30) dropped, second kept


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
