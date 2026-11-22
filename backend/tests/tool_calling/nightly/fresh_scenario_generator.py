"""Fresh scenario generator - uses LLM to create new test scenarios."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from ..lm_studio_client import create_lm_studio_client
from ..scenario_library import Scenario


@dataclass
class GenerationPrompt:
    """Prompt template for generating fresh scenarios."""

    category: str
    tool_names: list[str]
    template: str
    examples: list[str]


class FreshScenarioGenerator:
    """Generates fresh test scenarios using the LLM itself."""

    # Prompt templates for each category
    CATEGORY_PROMPTS = {
        "core": GenerationPrompt(
            category="core",
            tool_names=[
                "fs_read",
                "fs_write",
                "fs_edit",
                "fs_delete",
                "fs_list",
                "fs_glob",
                "fs_grep",
                "web_search",
                "url_read",
                "bash",
                "sandbox_execute",
                "terminal_open",
                "todo_write",
                "artifact_create",
            ],
            template="""Create a realistic user prompt that would require using the following tools: {tools}

The prompt should:
1. Be a natural user request (not a test instruction)
2. Require 2-5 tool calls to complete
3. Test a realistic workflow
4. Not be identical to existing scenarios

Examples of existing scenarios:
{examples}

Generate ONE new scenario as JSON:
{{
  "id": "core_fresh_001",
  "category": "core",
  "priority": 0,
  "prompt": "User request here...",
  "expected_tools": ["tool1", "tool2"],
  "min_tool_calls": 2,
  "max_tool_calls": 5,
  "max_turns": 6,
  "setup": {{}},
  "assertions": {{"no_errors": true}},
  "tags": ["fresh", "generated"],
  "description": "Auto-generated fresh scenario"
}}""",
            examples=[],
        ),
        "advanced": GenerationPrompt(
            category="advanced",
            tool_names=[
                "plan_submit",
                "plan_read",
                "goal_set",
                "goal_read",
                "job_create",
                "job_list",
                "workflow_run",
                "delegate_task",
                "skill_list",
                "ralph_run",
                "session_search",
                "spill_session",
                "schedule_create",
                "type_graph",
                "lsp",
                "file_reference",
                "run_code",
                "present",
                "office_process",
                "hook_run",
                "instructions_read",
                "team_task_create",
                "list_mcp_resources",
                "runtime_discover",
            ],
            template="""Create a realistic user prompt for advanced planning/automation tools: {tools}

The prompt should:
1. Be a natural user request for a complex task
2. Require 2-6 tool calls
3. Test planning, workflow, or advanced features
4. Not duplicate existing scenarios

Examples:
{examples}

Generate ONE new scenario as JSON (same format as core).""",
            examples=[],
        ),
        "chaining": GenerationPrompt(
            category="chaining",
            tool_names=[
                "web_search",
                "url_read",
                "fs_write",
                "fs_read",
                "sandbox_execute",
                "bash",
                "artifact_create",
                "todo_write",
                "terminal_open",
                "terminal_write",
            ],
            template="""Create a multi-step workflow prompt that chains multiple tools: {tools}

The prompt should:
1. Describe a realistic end-to-end workflow
2. Require 4-8 tool calls in sequence
3. Test tool chaining (output of one feeds into next)
4. Cover research, development, or data processing flows

Examples:
{examples}

Generate ONE new scenario as JSON.""",
            examples=[],
        ),
        "error": GenerationPrompt(
            category="error",
            tool_names=["fs_read", "fs_write", "bash", "web_search", "sandbox_execute", "url_read"],
            template="""Create a prompt that would trigger tool errors and test error handling: {tools}

The prompt should:
1. Lead to a realistic error condition (missing file, failed command, timeout, etc.)
2. Test that the model handles the error gracefully
3. Require 1-3 tool calls
4. Not be malicious or destructive

Examples:
{examples}

Generate ONE new scenario as JSON.""",
            examples=[],
        ),
    }

    def __init__(
        self,
        lm_studio_url: str = "http://localhost:1234/v1",
        model: str = "spark-x2.6-1.7b",
    ):
        self.lm_studio_url = lm_studio_url
        self.model = model

    async def generate(
        self,
        count: int = 10,
        categories: list[str] | None = None,
        existing_scenarios: list[Scenario] | None = None,
    ) -> list[Scenario]:
        """Generate fresh scenarios using the LLM."""
        if categories is None:
            categories = ["core", "advanced", "chaining", "error"]

        # Build examples from existing scenarios
        existing_by_category = {}
        if existing_scenarios:
            for s in existing_scenarios:
                if s.category not in existing_by_category:
                    existing_by_category[s.category] = []
                existing_by_category[s.category].append(s.prompt[:200])

        scenarios = []
        async with create_lm_studio_client(
            base_url=self.lm_studio_url,
            model=self.model,
        ) as lm_studio:
            for i in range(count):
                category = random.choice(categories)
                prompt_config = self.CATEGORY_PROMPTS.get(category)
                if not prompt_config:
                    continue

                examples = existing_by_category.get(category, [])[:3]
                tools = random.sample(
                    prompt_config.tool_names, k=min(5, len(prompt_config.tool_names))
                )

                prompt = prompt_config.template.format(
                    tools=", ".join(tools),
                    examples="\n".join(f"- {ex}" for ex in examples) if examples else "None",
                )

                try:
                    response = await lm_studio.chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        tools=[],  # No tools for generation
                        temperature=0.7,
                        max_tokens=1000,
                    )

                    content = response.choices[0].message.content
                    if content:
                        # Extract JSON from response
                        scenario_data = self._extract_json(content)
                        if scenario_data:
                            # Ensure unique ID
                            scenario_data["id"] = f"{category}_fresh_{i:03d}"
                            scenario_data["category"] = category
                            scenario = Scenario.from_dict(scenario_data)
                            scenarios.append(scenario)

                except Exception as e:
                    print(f"Failed to generate scenario for {category}: {e}")
                    continue

        return scenarios

    def _extract_json(self, text: str) -> dict | None:
        """Extract JSON object from LLM response."""
        import re

        # Try to find JSON block
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        # Try parsing entire text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


# Fallback: Pre-defined fresh scenario templates (used if LLM generation fails)
FRESH_SCENARIO_TEMPLATES = {
    "core": [
        {
            "id": "core_fresh_read_write",
            "category": "core",
            "priority": 0,
            "prompt": "Create a config file at /workspace/app/config.json with database settings, then read it back to verify",
            "expected_tools": ["fs_write", "fs_read"],
            "min_tool_calls": 2,
            "max_tool_calls": 4,
            "max_turns": 5,
            "setup": {},
            "assertions": {"no_errors": True, "file_created": "/workspace/app/config.json"},
            "tags": ["fresh", "template"],
            "description": "Write then read config file",
        },
        {
            "id": "core_fresh_search_replace",
            "category": "core",
            "priority": 0,
            "prompt": "Find all occurrences of 'debug' in /workspace/src/ and replace with 'log'",
            "expected_tools": ["fs_grep", "fs_str_replace"],
            "min_tool_calls": 2,
            "max_tool_calls": 5,
            "max_turns": 6,
            "setup": {"create_files": {"/workspace/src/main.py": "debug('start')\ndebug('end')"}},
            "assertions": {
                "no_errors": True,
                "file_not_contains": {"/workspace/src/main.py": "debug"},
            },
            "tags": ["fresh", "template"],
            "description": "Search and replace across files",
        },
    ],
    "chaining": [
        {
            "id": "chaining_fresh_research_report",
            "category": "chaining",
            "priority": 0,
            "prompt": "Research 'TypeScript best practices 2024', read 2 articles, create a summary report with code examples",
            "expected_tools": ["web_search", "url_read", "fs_write", "artifact_create"],
            "min_tool_calls": 4,
            "max_tool_calls": 8,
            "max_turns": 10,
            "setup": {},
            "assertions": {"no_errors": True, "artifact_created": True},
            "tags": ["fresh", "template"],
            "description": "Research to report workflow",
        },
    ],
}
