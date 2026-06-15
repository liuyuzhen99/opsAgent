from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


BrowserActionType: TypeAlias = Literal[
    "open_url",
    "click",
    "hover",
    "type",
    "type_username",
    "type_password",
    "login_submit",
    "select",
    "press",
    "wait_for",
    "observe_page",
    "extract_text",
    "save_artifact",
    "finish",
]
RiskLevel = str
TaskState = str
ActionResultStatus = str


@dataclass(slots=True)
class InteractiveElement:
    element_id: str
    role: str
    input_type: str = ""
    name: str = ""
    text: str = ""
    title: str = ""
    href: str = ""
    placeholder: str = ""
    context: str = ""
    locator_strategy: str = ""
    is_enabled: bool = True
    is_visible: bool = True


@dataclass(slots=True)
class BrowserAction:
    type: BrowserActionType
    target_hint: str = ""
    target_id: str | None = None
    value: str | None = None
    expected_outcome: str = ""
    risk_level: RiskLevel = "safe_read"
    requires_confirmation: bool = False
    timeout_ms: int = 5000
    key: str = ""


@dataclass(slots=True)
class BrowserObservation:
    url: str = ""
    title: str = ""
    page_type: str = "unknown"
    interactive_elements: list[InteractiveElement] = field(default_factory=list)
    forms: list[dict[str, Any]] = field(default_factory=list)
    visible_messages: list[str] = field(default_factory=list)
    page_text: str = ""
    last_action_result: str = ""
    blocking_reason: str | None = None
    screenshot_path: str | None = None
    page_summary_path: str | None = None
    done_signals: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ActionResult:
    status: ActionResultStatus
    observation: BrowserObservation
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BrowserTaskSpec:
    start_url: str | None
    user_goal: str
    success_criteria: list[str] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    credential_ref: str | None = None
    credential_username: str | None = None
    credential_password: str | None = None
    requires_login: bool = False
    requires_remote_mutation: bool = False
    auto_plan: bool = True
    session_state_path: str | None = None
    confirmed_action: BrowserAction | None = None
    replay_actions: list[BrowserAction] = field(default_factory=list)
    site_key: str | None = None
    workflow: str | None = None
    workflow_fields: dict[str, Any] = field(default_factory=dict)
    site_config: dict[str, Any] = field(default_factory=dict)
    completed_action_keys: list[str] = field(default_factory=list)
    trace_enabled: bool = False
    video_enabled: bool = False
    browser_channel: str | None = None
    browser_slow_mo_ms: int = 0
    max_steps: int = 20
    max_consecutive_failures: int = 3
    repeated_action_threshold: int = 3
    actions: list[BrowserAction] = field(default_factory=list)
