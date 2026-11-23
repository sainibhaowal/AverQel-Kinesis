"""Parallel replay - runs fixture replay concurrently for CI speed."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..disposable_backend import DisposableBackend
from .assertions import AssertionResult, ReplayAssertions
from .fixture_loader import FixtureLoader, LoadedFixture
from .replay_engine import ReplayEngine, ReplayResult


@dataclass
class ReplayWorkerResult:
    """Result from a replay worker."""

    worker_id: int
    fixture: LoadedFixture
    result: ReplayResult
    assertions: list[AssertionResult]
    error: str | None = None


@dataclass
class ReplaySummary:
    """Summary of parallel replay run."""

    total_fixtures: int
    successful: int
    failed: int
    total_assertions: int
    passed_assertions: int
    failed_assertions: int
    total_duration_ms: float
    wall_time_seconds: float
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    by_worker: dict[int, dict[str, int]] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)


class ParallelReplay:
    """Runs fixture replay in parallel across multiple workers."""

    def __init__(
        self,
        num_workers: int = 4,
        fixture_dir: str = "fixtures",
        max_concurrent_tools: int = 10,
    ):
        self.num_workers = num_workers
        self.fixture_dir = fixture_dir
        self.max_concurrent_tools = max_concurrent_tools

        self._queue: asyncio.Queue[LoadedFixture] = asyncio.Queue()
        self._results: list[ReplayWorkerResult] = []
        self._workers: list[asyncio.Task] = []
        self._backends: list[DisposableBackend] = []
        self._loader = FixtureLoader(fixture_dir)

    async def initialize(self, categories: list[str] | None = None) -> int:
        """Initialize workers and load fixtures into queue."""
        # Create backends for each worker
        for _i in range(self.num_workers):
            backend = DisposableBackend()
            await backend.initialize()
            self._backends.append(backend)

        # Load fixtures
        all_fixtures = self._loader.load_all()
        if categories:
            all_fixtures = {k: v for k, v in all_fixtures.items() if k in categories}

        # Queue fixtures
        fixture_count = 0
        for _category, fixtures in all_fixtures.items():
            for fixture in fixtures:
                await self._queue.put(fixture)
                fixture_count += 1

        return fixture_count

    async def close(self) -> None:
        """Clean up worker backends."""
        for backend in self._backends:
            await backend.close()

    async def _worker(self, worker_id: int) -> None:
        """Worker coroutine - replays fixtures from queue."""
        backend = self._backends[worker_id]
        engine = ReplayEngine(backend, max_concurrent_tools=self.max_concurrent_tools)

        while True:
            try:
                fixture = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                break  # Queue empty

            try:
                result = await engine.replay_fixture(fixture)
                assertions = ReplayAssertions.run_all_assertions(result, fixture.scenario)

                self._results.append(
                    ReplayWorkerResult(
                        worker_id=worker_id,
                        fixture=fixture,
                        result=result,
                        assertions=assertions,
                    )
                )

            except Exception as e:
                self._results.append(
                    ReplayWorkerResult(
                        worker_id=worker_id,
                        fixture=fixture,
                        result=ReplayResult(
                            fixture=fixture,
                            success=False,
                            errors=[{"error": str(e)}],
                        ),
                        assertions=[],
                        error=str(e),
                    )
                )

            finally:
                self._queue.task_done()

    async def run(self) -> ReplaySummary:
        """Run all fixtures in parallel."""
        # Start workers
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(self.num_workers)]

        # Wait for completion
        start_time = time.time()
        await self._queue.join()
        await asyncio.gather(*self._workers, return_exceptions=True)
        wall_time = time.time() - start_time

        # Generate summary
        return self._generate_summary(wall_time)

    def _generate_summary(self, wall_time: float) -> ReplaySummary:
        """Generate summary from worker results."""
        total = len(self._results)
        successful = sum(1 for r in self._results if r.result.success)
        failed = total - successful

        total_assertions = 0
        passed_assertions = 0
        failed_assertions = 0

        for r in self._results:
            for a in r.assertions:
                total_assertions += 1
                if a.passed:
                    passed_assertions += 1
                else:
                    failed_assertions += 1

        total_duration = sum(r.result.duration_ms for r in self._results)

        # By category
        by_category = {}
        for r in self._results:
            cat = r.fixture.category
            if cat not in by_category:
                by_category[cat] = {"total": 0, "success": 0, "failed": 0}
            by_category[cat]["total"] += 1
            if r.result.success:
                by_category[cat]["success"] += 1
            else:
                by_category[cat]["failed"] += 1

        # By worker
        by_worker = {}
        for r in self._results:
            wid = r.worker_id
            if wid not in by_worker:
                by_worker[wid] = {"total": 0, "success": 0, "failed": 0}
            by_worker[wid]["total"] += 1
            if r.result.success:
                by_worker[wid]["success"] += 1
            else:
                by_worker[wid]["failed"] += 1

        return ReplaySummary(
            total_fixtures=total,
            successful=successful,
            failed=failed,
            total_assertions=total_assertions,
            passed_assertions=passed_assertions,
            failed_assertions=failed_assertions,
            total_duration_ms=total_duration,
            wall_time_seconds=wall_time,
            by_category=by_category,
            by_worker=by_worker,
            details=[
                {
                    "worker_id": r.worker_id,
                    "scenario_id": r.fixture.scenario.id,
                    "category": r.fixture.category,
                    "success": r.result.success,
                    "duration_ms": r.result.duration_ms,
                    "assertions_passed": sum(1 for a in r.assertions if a.passed),
                    "assertions_failed": sum(1 for a in r.assertions if not a.passed),
                    "errors": r.result.errors,
                    "error": r.error,
                }
                for r in self._results
            ],
        )


async def parallel_replay_fixtures(
    fixture_dir: str = "fixtures",
    num_workers: int = 4,
    categories: list[str] | None = None,
) -> ReplaySummary:
    """Convenience function for parallel replay."""
    replay = ParallelReplay(
        num_workers=num_workers,
        fixture_dir=fixture_dir,
    )

    try:
        fixture_count = await replay.initialize(categories)
        print(f"Loaded {fixture_count} fixtures for replay")

        if fixture_count == 0:
            print("No fixtures to replay!")
            return ReplaySummary(
                total_fixtures=0,
                successful=0,
                failed=0,
                total_assertions=0,
                passed_assertions=0,
                failed_assertions=0,
                total_duration_ms=0,
                wall_time_seconds=0,
            )

        summary = await replay.run()

        print(f"\nReplay complete in {summary.wall_time_seconds:.1f}s")
        print(f"Fixtures: {summary.successful}/{summary.total_fixtures} passed")
        print(f"Assertions: {summary.passed_assertions}/{summary.total_assertions} passed")

        if summary.failed > 0:
            print("\nFailed fixtures:")
            for d in summary.details:
                if not d["success"]:
                    print(
                        f"  - {d['scenario_id']} ({d['category']}): {d['error'] or 'assertion failures'}"
                    )

        return summary

    finally:
        await replay.close()
