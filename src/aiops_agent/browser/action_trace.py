from __future__ import annotations

from typing import Any


TRACE_SCHEMA_VERSION = "opsagent.web_action_trace.v1"
SECRET_ACTION_TYPES = {"type_username", "type_password", "login_submit"}
SENSITIVE_KEY_PARTS = ("password", "token", "credential", "secret", "api_key", "apikey", "cookie")


def build_canonical_action_trace(
    steps: list[dict[str, Any]],
    *,
    status: str,
    task_id: str = "",
    session_id: str = "",
    pending_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_steps = [_canonical_step(step, index + 1) for index, step in enumerate(steps)]
    artifact_paths = _artifact_paths(canonical_steps)
    payload: dict[str, Any] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "status": status,
        "task_id": task_id,
        "session_id": session_id,
        "step_count": len(canonical_steps),
        "steps": canonical_steps,
        "artifact_paths": artifact_paths,
    }
    if pending_action:
        payload["pending_action"] = _sanitize_action(pending_action)
    return payload


def legacy_steps_from_canonical_trace(trace: dict[str, Any]) -> list[dict[str, Any]]:
    steps = trace.get("steps") if isinstance(trace, dict) else []
    if not isinstance(steps, list):
        return []
    legacy_steps: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        legacy_steps.append(
            {
                "step_index": step.get("step_index"),
                "action": dict(step.get("action") or {}),
                "result": step.get("result") or "",
                "observation": dict(step.get("observation") or {}),
                "error": step.get("error"),
                "reflection": dict(step.get("reflection") or {}),
            }
        )
    return legacy_steps


def _canonical_step(step: dict[str, Any], fallback_index: int) -> dict[str, Any]:
    action = _sanitize_action(dict(step.get("action") or {}))
    observation = _sanitize_observation(dict(step.get("observation") or {}))
    reflection = _sanitize_value(step.get("reflection") or {})
    return {
        "step_index": int(step.get("step_index") or fallback_index),
        "action": action,
        "result": str(step.get("result") or ""),
        "risk_level": str(action.get("risk_level") or "read_only"),
        "requires_confirmation": bool(action.get("requires_confirmation", False)),
        "observation": observation,
        "error": _sanitize_text(step.get("error")),
        "reflection": reflection if isinstance(reflection, dict) else {},
    }


def _sanitize_action(action: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_value(action)
    if not isinstance(sanitized, dict):
        return {}
    action_type = str(sanitized.get("type") or "")
    target = " ".join(str(sanitized.get(key) or "") for key in ("target_hint", "target_id", "key")).lower()
    if action_type in SECRET_ACTION_TYPES or any(part in target for part in ("password", "密码")):
        if "value" in sanitized:
            sanitized["value"] = "***"
    return sanitized


def _sanitize_observation(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": _sanitize_text(observation.get("url")),
        "title": _sanitize_text(observation.get("title")),
        "page_type": _sanitize_text(observation.get("page_type")),
        "last_action_result": _sanitize_text(observation.get("last_action_result")),
        "blocking_reason": _sanitize_text(observation.get("blocking_reason")),
        "screenshot_path": _sanitize_text(observation.get("screenshot_path")),
        "page_summary_path": _sanitize_text(observation.get("page_summary_path")),
        "visible_messages": [
            _sanitize_text(item)
            for item in list(observation.get("visible_messages") or [])[:10]
        ],
        "element_count": len(observation.get("interactive_elements") or []),
    }


def _artifact_paths(steps: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for step in steps:
        observation = step.get("observation") or {}
        for key in ("screenshot_path", "page_summary_path"):
            path = observation.get(key)
            if path and path not in paths:
                paths.append(str(path))
    return paths


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                sanitized[key_text] = "***"
            else:
                sanitized[key_text] = _sanitize_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value[:50]]
    if isinstance(value, (bool, int, float)):
        return value
    return _sanitize_text(value)


def _sanitize_text(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if len(text) > 1200:
        text = text[:1199].rstrip() + "…"
    return text


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)
