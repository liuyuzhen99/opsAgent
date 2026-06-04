from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from aiops_agent.browser.models import (
    ActionResult,
    BrowserAction,
    BrowserObservation,
    BrowserTaskSpec,
    InteractiveElement,
)
from aiops_agent.sessions.models import (
    AgentSession,
    BrowserMemory,
    PageMemory,
    QATurn,
    SessionTaskIndexEntry,
    ShortTermTurn,
)
from aiops_agent.tasks.models import (
    ExecutionPlan,
    Task,
    TaskArtifact,
    ToolCallSpec,
    ToolExecutionResult,
)


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain(item) for item in value]
    if hasattr(value, "page_content") and hasattr(value, "metadata"):
        return {
            "page_content": str(getattr(value, "page_content", "")),
            "metadata": to_plain(getattr(value, "metadata", {}) or {}),
        }
    return str(value)


def task_to_state(task: Task | dict[str, Any]) -> dict[str, Any]:
    return to_plain(task if isinstance(task, dict) else task)


def task_from_state(value: Task | dict[str, Any]) -> Task:
    if isinstance(value, Task):
        return value
    raw = dict(value)
    raw["artifacts"] = [artifact_from_state(item) for item in raw.get("artifacts") or []]
    raw["tool_calls"] = [tool_call_from_state(item) for item in raw.get("tool_calls") or []]
    if raw.get("plan"):
        raw["plan"] = execution_plan_from_state(raw["plan"])
    return Task(**raw)


def execution_plan_from_state(value: ExecutionPlan | dict[str, Any]) -> ExecutionPlan:
    if isinstance(value, ExecutionPlan):
        return value
    raw = dict(value)
    raw["tool_calls"] = [tool_call_from_state(item) for item in raw.get("tool_calls") or []]
    return ExecutionPlan(**raw)


def tool_call_to_state(call: ToolCallSpec | dict[str, Any]) -> dict[str, Any]:
    return to_plain(call if isinstance(call, dict) else call)


def tool_call_from_state(value: ToolCallSpec | dict[str, Any]) -> ToolCallSpec:
    if isinstance(value, ToolCallSpec):
        return value
    return ToolCallSpec(**dict(value))


def artifact_to_state(artifact: TaskArtifact | dict[str, Any]) -> dict[str, Any]:
    return to_plain(artifact if isinstance(artifact, dict) else artifact)


def artifact_from_state(value: TaskArtifact | dict[str, Any]) -> TaskArtifact:
    if isinstance(value, TaskArtifact):
        return value
    return TaskArtifact(**dict(value))


def tool_result_to_state(result: ToolExecutionResult | dict[str, Any]) -> dict[str, Any]:
    return to_plain(result if isinstance(result, dict) else result)


def tool_result_from_state(value: ToolExecutionResult | dict[str, Any]) -> ToolExecutionResult:
    if isinstance(value, ToolExecutionResult):
        return value
    raw = dict(value)
    raw["artifacts"] = [artifact_from_state(item) for item in raw.get("artifacts") or []]
    raw.setdefault("data", {})
    return ToolExecutionResult(**raw)


def session_to_state(session: AgentSession | dict[str, Any]) -> dict[str, Any]:
    return to_plain(session if isinstance(session, dict) else session)


def session_from_state(value: AgentSession | dict[str, Any]) -> AgentSession:
    if isinstance(value, AgentSession):
        return value
    raw = dict(value)
    raw["short_term"] = [ShortTermTurn(**dict(item)) for item in raw.get("short_term") or []]
    raw["browser_memory"] = browser_memory_from_state(raw.get("browser_memory") or {})
    raw["qa_memory"] = [QATurn(**dict(item)) for item in raw.get("qa_memory") or []]
    raw["task_index"] = [SessionTaskIndexEntry(**dict(item)) for item in raw.get("task_index") or []]
    return AgentSession(**raw)


def browser_memory_from_state(value: BrowserMemory | dict[str, Any]) -> BrowserMemory:
    if isinstance(value, BrowserMemory):
        return value
    raw = dict(value)
    raw["recent_pages"] = [PageMemory(**dict(item)) for item in raw.get("recent_pages") or []]
    return BrowserMemory(**raw)


def browser_action_to_state(action: BrowserAction | dict[str, Any]) -> dict[str, Any]:
    return to_plain(action if isinstance(action, dict) else action)


def browser_action_from_state(value: BrowserAction | dict[str, Any]) -> BrowserAction:
    if isinstance(value, BrowserAction):
        return value
    return BrowserAction(**dict(value))


def browser_observation_to_state(observation: BrowserObservation | dict[str, Any]) -> dict[str, Any]:
    return to_plain(observation if isinstance(observation, dict) else observation)


def browser_observation_from_state(value: BrowserObservation | dict[str, Any]) -> BrowserObservation:
    if isinstance(value, BrowserObservation):
        return value
    raw = dict(value)
    raw["interactive_elements"] = [
        item if isinstance(item, InteractiveElement) else InteractiveElement(**dict(item))
        for item in raw.get("interactive_elements") or []
    ]
    return BrowserObservation(**raw)


def browser_task_spec_to_state(spec: BrowserTaskSpec | dict[str, Any]) -> dict[str, Any]:
    return to_plain(spec if isinstance(spec, dict) else spec)


def browser_task_spec_from_state(value: BrowserTaskSpec | dict[str, Any]) -> BrowserTaskSpec:
    if isinstance(value, BrowserTaskSpec):
        return value
    raw = dict(value)
    if raw.get("confirmed_action"):
        raw["confirmed_action"] = browser_action_from_state(raw["confirmed_action"])
    raw["replay_actions"] = [browser_action_from_state(item) for item in raw.get("replay_actions") or []]
    raw["actions"] = [browser_action_from_state(item) for item in raw.get("actions") or []]
    return BrowserTaskSpec(**raw)


def action_result_to_state(result: ActionResult | dict[str, Any]) -> dict[str, Any]:
    return to_plain(result if isinstance(result, dict) else result)


def action_result_from_state(value: ActionResult | dict[str, Any]) -> ActionResult:
    if isinstance(value, ActionResult):
        return value
    raw = dict(value)
    raw["observation"] = browser_observation_from_state(raw.get("observation") or {})
    return ActionResult(**raw)


def web_step_result_to_state(value: Any) -> Any:
    if isinstance(value, ToolExecutionResult):
        return {"kind": "tool_result", "value": tool_result_to_state(value)}
    if isinstance(value, tuple) and len(value) == 2:
        return {
            "kind": "action_observation",
            "action_result": action_result_to_state(value[0]),
            "observation": browser_observation_to_state(value[1]),
        }
    return to_plain(value)


def web_step_result_from_state(value: Any) -> Any:
    if isinstance(value, ToolExecutionResult) or isinstance(value, tuple):
        return value
    if not isinstance(value, dict):
        return value
    kind = value.get("kind")
    if kind == "tool_result":
        return tool_result_from_state(value.get("value") or {})
    if kind == "action_observation":
        return (
            action_result_from_state(value.get("action_result") or {}),
            browser_observation_from_state(value.get("observation") or {}),
        )
    if "success" in value and "data" in value:
        return tool_result_from_state(value)
    return value
