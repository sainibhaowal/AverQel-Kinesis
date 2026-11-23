"""Parallel recorder - runs multiple scenarios concurrently with worker pool."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from ..disposable_backend import DisposableBackend
from ..lm_studio_client import (
    LMStudioClient,
    create_lm_studio_client,
)
from ..scenario_library import Scenario
from .fixture_serializer import FixtureSerializer
from .scenario_executor import ExecutionResult, ScenarioExecutor


@dataclass
class WorkerResult:
    """Result from a worker."""

    worker_id: int
    scenario_id: str
    result: ExecutionResult
    error: str | None = None


class WorkerPool:
    """Manages a pool of workers for parallel scenario execution."""

    def __init__(
        self,
        num_workers: int,
        lm_studio_url: str,
        model: str,
        max_turns: int = 20,
        max_tool_calls_per_turn: int = 10,
        fixture_dir: str = "fixtures",
    ):
        self.num_workers = num_workers
        self.lm_studio_url = lm_studio_url
        self.model = model
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.fixture_dir = fixture_dir

        self._queue: asyncio.Queue[Scenario] = asyncio.Queue()
        self._results: list[WorkerResult] = []
        self._workers: list[asyncio.Task] = []
        self._serializer = FixtureSerializer(fixture_dir)
        self._lm_studio_clients: list[LMStudioClient] = []
        self._backends: list[DisposableBackend] = []

    async def initialize(self) -> None:
        """Initialize worker resources (LM Studio clients, backends)."""
        for _i in range(self.num_workers):
            # Create LM Studio client per worker
            lm_studio = create_lm_studio_client(
                base_url=self.lm_studio_url,
                model=self.model,
            )
            await lm_studio.__aenter__()
            self._lm_studio_clients.append(lm_studio)

            # Create backend per worker
            backend = DisposableBackend()
            await backend.initialize()
            self._backends.append(backend)

    async def close(self) -> None:
        """Clean up all worker resources."""
        for backend in self._backends:
            await backend.close()
        for lm_studio in self._lm_studio_clients:
            await lm_studio.__aexit__(None, None, None)

    async def _worker(self, worker_id: int) -> None:
        """Worker coroutine - processes scenarios from queue."""
        lm_studio = self._lm_studio_clients[worker_id]
        backend = self._backends[worker_id]

        executor = ScenarioExecutor(
            lm_studio=lm_studio,
            backend=backend,
            max_turns=self.max_turns,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
        )

        while True:
            try:
                scenario = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                break  # Queue empty, worker done

            try:
                result = await executor.execute(scenario)

                # Save trace if successful
                if result.success and result.trace:
                    self._serializer.save_trace(result.trace, scenario.category)

                self._results.append(
                    WorkerResult(
                        worker_id=worker_id,
                        scenario_id=scenario.id,
                        result=result,
                    )
                )

            except Exception as e:
                self._results.append(
                    WorkerResult(
                        worker_id=worker_id,
                        scenario_id=scenario.id,
                        result=ExecutionResult(
                            scenario=scenario,
                            trace=None,
                            success=False,
                            error=str(e),
                        ),
                        error=str(e),
                    )
                )

            finally:
                self._queue.task_done()

    async def run_scenarios(self, scenarios: list[Scenario]) -> list[WorkerResult]:
        """Run all scenarios in parallel across workers."""
        # Load queue
        for scenario in scenarios:
            await self._queue.put(scenario)

        # Start workers
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(self.num_workers)]

        # Wait for queue to be processed
        await self._queue.join()

        # Wait for workers to finish
        await asyncio.gather(*self._workers, return_exceptions=True)

        return self._results

    def summary(self) -> dict[str, Any]:
        """Generate summary of parallel run."""
        total = len(self._results)
        successful = sum(1 for r in self._results if r.result.success)
        failed = total - successful
        total_time = sum(r.result.duration_ms for r in self._results)

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

        by_category = {}
        for r in self._results:
            cat = r.result.scenario.category
            if cat not in by_category:
                by_category[cat] = {"total": 0, "success": 0, "failed": 0}
            by_category[cat]["total"] += 1
            if r.result.success:
                by_category[cat]["success"] += 1
            else:
                by_category[cat]["failed"] += 1

        return {
            "total_scenarios": total,
            "successful": successful,
            "failed": failed,
            "success_rate": successful / total if total > 0 else 0,
            "total_duration_ms": total_time,
            "by_worker": by_worker,
            "by_category": by_category,
            "results": [
                {
                    "worker_id": r.worker_id,
                    "scenario_id": r.scenario_id,
                    "success": r.result.success,
                    "duration_ms": r.result.duration_ms,
                    "error": r.result.error,
                }
                for r in self._results
            ],
        }


class ParallelRecorder:
    """High-level parallel recorder orchestrating the full recording process."""

    def __init__(
        self,
        num_workers: int = 4,
        lm_studio_url: str = "http://localhost:1234/v1",
        model: str = "spark-x2.6-1.7b",
        max_turns: int = 20,
        max_tool_calls_per_turn: int = 10,
        fixture_dir: str = "fixtures",
    ):
        self.num_workers = num_workers
        self.lm_studio_url = lm_studio_url
        self.model = model
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.fixture_dir = fixture_dir
        self.pool: WorkerPool | None = None

    async def __aenter__(self) -> ParallelRecorder:
        self.pool = WorkerPool(
            num_workers=self.num_workers,
            lm_studio_url=self.lm_studio_url,
            model=self.model,
            max_turns=self.max_turns,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
            fixture_dir=self.fixture_dir,
        )
        await self.pool.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.pool:
            await self.pool.close()

    async def record(self, scenarios: list[Scenario]) -> dict[str, Any]:
        """Record all scenarios in parallel."""
        if not self.pool:
            raise RuntimeError("ParallelRecorder not initialized. Use async context manager.")

        print(f"Starting parallel recording with {self.num_workers} workers...")
        print(f"Total scenarios: {len(scenarios)}")

        start_time = time.time()
        await self.pool.run_scenarios(scenarios)
        total_time = time.time() - start_time

        summary = self.pool.summary()
        summary["wall_time_seconds"] = total_time

        print(f"\nRecording complete in {total_time:.1f}s")
        print(f"Successful: {summary['successful']}/{summary['total_scenarios']}")
        print(f"Failed: {summary['failed']}")

        return summary


async def parallel_record_scenarios(
    scenario_files: list[str],
    num_workers: int = 4,
    lm_studio_url: str = "http://localhost:1234/v1",
    model: str = "spark-x2.6-1.7b",
    fixture_dir: str = "fixtures",
    categories: list[str] | None = None,
    priorities: list[int] | None = None,
) -> dict[str, Any]:
    """Convenience function for parallel recording."""
    from ..scenario_library import load_scenarios

    # Load all scenarios
    all_scenarios = []
    for f in scenario_files:
        scenarios = load_scenarios(f)
        all_scenarios.extend(scenarios)

    # Filter
    if categories:
        all_scenarios = [s for s in all_scenarios if s.category in categories]
    if priorities:
        all_scenarios = [s for s in all_scenarios if s.priority in priorities]

    # Sort
    all_scenarios.sort(key=lambda s: (s.priority, s.category, s.id))

    async with ParallelRecorder(
        num_workers=num_workers,
        lm_studio_url=lm_studio_url,
        model=model,
        fixture_dir=fixture_dir,
    ) as recorder:
        return await recorder.record(all_scenarios)
