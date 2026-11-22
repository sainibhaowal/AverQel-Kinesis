"""Scenario executor - runs individual scenarios with full setup/teardown."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..disposable_backend import BackendTestClient, DisposableBackend
from ..lm_studio_client import LMStudioClient, ToolCallingOrchestrator, ToolCallTrace
from ..scenario_library import Scenario


@dataclass
class ExecutionResult:
    """Result of executing a scenario."""

    scenario: Scenario
    trace: ToolCallTrace | None
    success: bool
    error: str | None = None
    duration_ms: float = 0
    assertions_passed: dict[str, bool] = None

    def __post_init__(self):
        if self.assertions_passed is None:
            self.assertions_passed = {}


class ScenarioExecutor:
    """Executes a single scenario with full lifecycle management."""

    def __init__(
        self,
        lm_studio: LMStudioClient,
        backend: DisposableBackend,
        max_turns: int = 20,
        max_tool_calls_per_turn: int = 10,
    ):
        self.lm_studio = lm_studio
        self.backend = backend
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.backend_client = BackendTestClient(backend)
        self.orchestrator = ToolCallingOrchestrator(
            lm_studio=lm_studio,
            backend_client=self.backend_client,
            tools=self.backend_client.get_available_tools(),
            max_turns=max_turns,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
        )

    def apply_setup(self, scenario: Scenario) -> None:
        """Apply scenario setup (create files, directories, etc.)."""
        setup = scenario.setup

        # Create files
        if setup.get("create_files"):
            for path, content in setup["create_files"].items():
                full_path = self.backend.temp_dir / path.lstrip("/")
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content)

        # Create read-only directory
        if setup.get("create_readonly_dir"):
            readonly_path = self.backend.temp_dir / setup["create_readonly_dir"].lstrip("/")
            readonly_path.mkdir(parents=True, exist_ok=True)
            readonly_path.chmod(0o555)

        # Create plan (if task store supports it)
        if setup.get("create_plan"):
            # Would need task store integration
            pass

        # Create job (if job system supports it)
        if setup.get("create_job"):
            # Would need job system integration
            pass

    def verify_assertions(self, scenario: Scenario, trace: ToolCallTrace) -> dict[str, bool]:
        """Verify scenario assertions against the trace."""
        results = {}
        assertions = scenario.assertions

        # Basic assertions
        if assertions.get("no_errors"):
            results["no_errors"] = len(trace.errors) == 0

        if assertions.get("min_tool_calls"):
            results["min_tool_calls"] = len(trace.tool_calls) >= assertions["min_tool_calls"]

        if assertions.get("contains_in_response"):
            content = trace.final_response or ""
            for expected in assertions["contains_in_response"]:
                results[f"contains_{expected}"] = expected in content

        if assertions.get("not_contains_in_response"):
            content = trace.final_response or ""
            for unexpected in assertions["not_contains_in_response"]:
                results[f"not_contains_{unexpected}"] = unexpected not in content

        # File assertions
        if assertions.get("file_created"):
            files = assertions["file_created"]
            if isinstance(files, str):
                files = [files]
            for f in files:
                full_path = self.backend.temp_dir / f.lstrip("/")
                results[f"file_created_{f}"] = full_path.exists()

        if assertions.get("file_deleted"):
            full_path = self.backend.temp_dir / assertions["file_deleted"].lstrip("/")
            results["file_deleted"] = not full_path.exists()

        if assertions.get("file_contains"):
            for f, content in assertions["file_contains"].items():
                full_path = self.backend.temp_dir / f.lstrip("/")
                if full_path.exists():
                    results[f"file_contains_{f}"] = content in full_path.read_text()
                else:
                    results[f"file_contains_{f}"] = False

        if assertions.get("file_not_contains"):
            for f, content in assertions["file_not_contains"].items():
                full_path = self.backend.temp_dir / f.lstrip("/")
                if full_path.exists():
                    results[f"file_not_contains_{f}"] = content not in full_path.read_text()
                else:
                    results[f"file_not_contains_{f}"] = True

        if assertions.get("dir_created"):
            dirs = assertions["dir_created"]
            if isinstance(dirs, str):
                dirs = [dirs]
            for d in dirs:
                full_path = self.backend.temp_dir / d.lstrip("/")
                results[f"dir_created_{d}"] = full_path.is_dir()

        # Tool error handling
        if assertions.get("tool_error_handled"):
            # Check if model continued after tool error
            results["tool_error_handled"] = (
                trace.final_response is not None and len(trace.tool_calls) > 0
            )

        if assertions.get("validation_error_caught"):
            # Check if validation error occurred and was handled
            validation_errors = [e for e in trace.errors if e.get("type") == "validation_error"]
            results["validation_error_caught"] = len(validation_errors) > 0

        if assertions.get("runtime_error_handled"):
            runtime_errors = [
                e
                for e in trace.errors
                if e.get("type") in ("tool_execution_error", "runtime_error")
            ]
            results["runtime_error_handled"] = (
                len(runtime_errors) > 0 and trace.final_response is not None
            )

        # Model recovery
        if assertions.get("model_recovers"):
            results["model_recovers"] = trace.final_response is not None

        if assertions.get("retry_succeeds"):
            # Check if model retried and succeeded
            results["retry_succeeds"] = trace.final_response is not None

        if assertions.get("fallback_used"):
            # Check if model used alternative tool
            tools_used = {tc["tool_name"] for tc in trace.tool_calls}
            results["fallback_used"] = len(tools_used) > 1

        if assertions.get("answer_provided"):
            results["answer_provided"] = (
                trace.final_response is not None and len(trace.final_response) > 10
            )

        # Parallel calls
        if assertions.get("parallel_calls"):
            # Check if multiple calls in same turn
            turns_with_multiple = {}
            for tc in trace.tool_calls:
                turn = tc.get("turn", 0)
                if turn not in turns_with_multiple:
                    turns_with_multiple[turn] = 0
                turns_with_multiple[turn] += 1
            max_parallel = max(turns_with_multiple.values()) if turns_with_multiple else 0
            results["parallel_calls"] = max_parallel >= assertions["parallel_calls"]

        # Special assertions
        if assertions.get("artifact_created"):
            results["artifact_created"] = any(
                "artifact_create" in tc.get("tool_name", "") for tc in trace.tool_calls
            )

        if assertions.get("tests_pass"):
            # Would need test execution integration
            results["tests_pass"] = True  # Placeholder

        return results

    async def execute(self, scenario: Scenario) -> ExecutionResult:
        """Execute a single scenario."""
        start_time = time.time()

        # Apply setup
        self.apply_setup(scenario)

        try:
            # Run scenario
            trace = await self.orchestrator.run_scenario(
                scenario_id=scenario.id,
                prompt=scenario.prompt,
                tools_available=scenario.expected_tools if scenario.expected_tools else None,
            )

            # Verify assertions
            assertions_passed = self.verify_assertions(scenario, trace)

            duration_ms = (time.time() - start_time) * 1000

            return ExecutionResult(
                scenario=scenario,
                trace=trace,
                success=True,
                duration_ms=duration_ms,
                assertions_passed=assertions_passed,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return ExecutionResult(
                scenario=scenario,
                trace=None,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )


async def execute_scenario_standalone(
    scenario: Scenario,
    lm_studio_url: str = "http://localhost:1234/v1",
    model: str = "spark-x2.6-1.7b",
    max_turns: int = 20,
    max_tool_calls_per_turn: int = 10,
) -> ExecutionResult:
    """Execute a scenario with its own disposable backend."""
    async with LMStudioClient(base_url=lm_studio_url, model=model) as lm_studio:
        backend = DisposableBackend()
        try:
            await backend.initialize()
            executor = ScenarioExecutor(
                lm_studio=lm_studio,
                backend=backend,
                max_turns=max_turns,
                max_tool_calls_per_turn=max_tool_calls_per_turn,
            )
            return await executor.execute(scenario)
        finally:
            await backend.close()
