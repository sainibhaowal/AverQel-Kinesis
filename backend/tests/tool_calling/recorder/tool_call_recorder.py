"""Main recording orchestration for tool-calling fixtures.

This is Approach #2: Record real LLM + real backend interactions as fixtures
for deterministic CI replay.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..disposable_backend import BackendTestClient, DisposableBackend
from ..lm_studio_client import (
    LMStudioClient,
    ToolCallingOrchestrator,
    ToolCallTrace,
    create_lm_studio_client,
)
from ..scenario_library import Scenario, load_scenarios


@dataclass
class RecordingResult:
    """Result of recording one scenario."""

    scenario_id: str
    success: bool
    trace: ToolCallTrace | None = None
    error: str | None = None
    duration_ms: float = 0
    fixture_path: str | None = None


class ToolCallRecorder:
    """Records tool-calling scenarios by running them against real LM Studio + backend."""

    def __init__(
        self,
        lm_studio_url: str = "http://localhost:1234/v1",
        model: str = "spark-x2.6-1.7b",
        timeout: float = 60.0,
        max_turns: int = 20,
        max_tool_calls_per_turn: int = 10,
        fixture_dir: str = "fixtures",
    ):
        self.lm_studio_url = lm_studio_url
        self.model = model
        self.timeout = timeout
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.fixture_dir = Path(fixture_dir)
        self.fixture_dir.mkdir(parents=True, exist_ok=True)

        self._lm_studio: LMStudioClient | None = None
        self._backend: DisposableBackend | None = None
        self._orchestrator: ToolCallingOrchestrator | None = None

    async def __aenter__(self) -> ToolCallRecorder:
        # Initialize LM Studio client
        self._lm_studio = create_lm_studio_client(
            base_url=self.lm_studio_url,
            model=self.model,
        )
        await self._lm_studio.__aenter__()

        # Verify LM Studio is reachable
        if not await self._lm_studio.health_check():
            raise RuntimeError(f"LM Studio not reachable at {self.lm_studio_url}")

        # Initialize disposable backend
        self._backend = DisposableBackend()
        await self._backend.initialize()

        # Create backend client wrapper
        backend_client = BackendTestClient(self._backend)

        # Get available tools from backend
        available_tools = backend_client.get_available_tools()

        # Create orchestrator
        self._orchestrator = ToolCallingOrchestrator(
            lm_studio=self._lm_studio,
            backend_client=backend_client,
            tools=available_tools,
            max_turns=self.max_turns,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
        )

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._backend:
            await self._backend.close()
        if self._lm_studio:
            await self._lm_studio.__aexit__(exc_type, exc_val, exc_tb)

    async def record_scenario(self, scenario: Scenario) -> RecordingResult:
        """Record a single scenario."""
        start_time = time.time()

        # Apply scenario setup
        if scenario.setup.get("create_files"):
            for path, content in scenario.setup["create_files"].items():
                full_path = self._backend.temp_dir / path.lstrip("/")
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content)

        if scenario.setup.get("create_readonly_dir"):
            readonly_path = self._backend.temp_dir / scenario.setup["create_readonly_dir"].lstrip(
                "/"
            )
            readonly_path.mkdir(parents=True, exist_ok=True)
            readonly_path.chmod(0o555)  # Read-only

        # Run the scenario
        try:
            trace = await self._orchestrator.run_scenario(
                scenario_id=scenario.id,
                prompt=scenario.prompt,
                tools_available=scenario.expected_tools if scenario.expected_tools else None,
            )

            # Add scenario metadata to trace
            trace.metadata.update(
                {
                    "scenario_category": scenario.category,
                    "scenario_priority": scenario.priority,
                    "expected_tools": scenario.expected_tools,
                    "min_tool_calls": scenario.min_tool_calls,
                    "max_tool_calls": scenario.max_tool_calls,
                    "max_turns": scenario.max_turns,
                    "assertions": scenario.assertions,
                    "tags": scenario.tags,
                    "description": scenario.description,
                    "model": self.model,
                    "lm_studio_url": self.lm_studio_url,
                }
            )

            duration_ms = (time.time() - start_time) * 1000

            return RecordingResult(
                scenario_id=scenario.id,
                success=True,
                trace=trace,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return RecordingResult(
                scenario_id=scenario.id,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )

    def save_trace(self, trace: ToolCallTrace, category: str = "core") -> str:
        """Save a trace as a JSONL fixture file."""
        fixture_file = self.fixture_dir / f"{category}_tools.jsonl"

        # Append to JSONL file
        with open(fixture_file, "a", encoding="utf-8") as f:
            f.write(trace.to_jsonl() + "\n")

        return str(fixture_file)


class RecordingSession:
    """Manages a full recording session across multiple scenarios."""

    def __init__(
        self,
        scenarios: list[Scenario],
        lm_studio_url: str = "http://localhost:1234/v1",
        model: str = "spark-x2.6-1.7b",
        fixture_dir: str = "fixtures",
        max_turns: int = 20,
        max_tool_calls_per_turn: int = 10,
    ):
        self.scenarios = scenarios
        self.lm_studio_url = lm_studio_url
        self.model = model
        self.fixture_dir = fixture_dir
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.results: list[RecordingResult] = []

    async def run(self) -> list[RecordingResult]:
        """Run all scenarios in sequence."""
        async with ToolCallRecorder(
            lm_studio_url=self.lm_studio_url,
            model=self.model,
            max_turns=self.max_turns,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
            fixture_dir=self.fixture_dir,
        ) as recorder:
            for scenario in self.scenarios:
                print(f"Recording {scenario.id} ({scenario.category}, P{scenario.priority})...")
                result = await recorder.record_scenario(scenario)
                self.results.append(result)

                if result.success and result.trace:
                    recorder.save_trace(result.trace, scenario.category)
                    print(f"  ✓ Success ({result.duration_ms:.0f}ms)")
                else:
                    print(f"  ✗ Failed: {result.error}")

        return self.results

    def summary(self) -> dict[str, Any]:
        """Generate summary of recording session."""
        total = len(self.results)
        successful = sum(1 for r in self.results if r.success)
        failed = total - successful
        total_time = sum(r.duration_ms for r in self.results)

        by_category = {}
        for r in self.results:
            # Find scenario category
            scenario = next((s for s in self.scenarios if s.id == r.scenario_id), None)
            cat = scenario.category if scenario else "unknown"
            if cat not in by_category:
                by_category[cat] = {"total": 0, "success": 0, "failed": 0}
            by_category[cat]["total"] += 1
            if r.success:
                by_category[cat]["success"] += 1
            else:
                by_category[cat]["failed"] += 1

        return {
            "total_scenarios": total,
            "successful": successful,
            "failed": failed,
            "success_rate": successful / total if total > 0 else 0,
            "total_duration_ms": total_time,
            "by_category": by_category,
            "results": [
                {
                    "scenario_id": r.scenario_id,
                    "success": r.success,
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                }
                for r in self.results
            ],
        }


async def record_all_scenarios(
    scenario_files: list[str],
    lm_studio_url: str = "http://localhost:1234/v1",
    model: str = "spark-x2.6-1.7b",
    fixture_dir: str = "fixtures",
    categories: list[str] | None = None,
    priorities: list[int] | None = None,
) -> dict[str, Any]:
    """Convenience function to record all scenarios from YAML files."""

    # Load all scenarios
    all_scenarios = []
    for f in scenario_files:
        scenarios = load_scenarios(f)
        all_scenarios.extend(scenarios)

    # Filter by category/priority if specified
    if categories:
        all_scenarios = [s for s in all_scenarios if s.category in categories]
    if priorities:
        all_scenarios = [s for s in all_scenarios if s.priority in priorities]

    # Sort by priority then category
    all_scenarios.sort(key=lambda s: (s.priority, s.category, s.id))

    print(f"Recording {len(all_scenarios)} scenarios...")

    session = RecordingSession(
        scenarios=all_scenarios,
        lm_studio_url=lm_studio_url,
        model=model,
        fixture_dir=fixture_dir,
    )

    await session.run()
    return session.summary()
