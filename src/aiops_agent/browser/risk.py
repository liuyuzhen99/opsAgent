from __future__ import annotations

from aiops_agent.browser.models import BrowserAction, BrowserObservation


UNSAFE_HINTS = (
    "submit",
    "save",
    "delete",
    "remove",
    "create",
    "grant",
    "revoke",
    "upload",
    "download",
    "提交",
    "保存",
    "删除",
    "创建",
    "开通",
    "撤销",
    "上传",
    "下载",
)


class RiskEvaluator:
    def classify(self, action: BrowserAction, observation: BrowserObservation | None = None) -> str:
        if action.risk_level in {"unsafe_mutation", "unknown_risk"}:
            return action.risk_level
        if action.type in {"open_url", "observe_page", "wait_for", "extract_text", "save_artifact", "finish"}:
            return "safe_read"
        if action.type == "login_submit":
            return "safe_local_edit"
        if action.type == "press":
            return "safe_local_edit"
        if action.type == "click" and self._looks_like_date_field(action):
            return "safe_local_edit"
        if action.type in {"type", "select"}:
            searchable = " ".join(
                item
                for item in (action.type, action.value or "", action.expected_outcome)
                if item
            ).lower()
            if any(hint in searchable for hint in UNSAFE_HINTS):
                return "unsafe_mutation"
            return "safe_local_edit"
        searchable = " ".join(
            item
            for item in (action.type, action.target_hint, action.value or "", action.expected_outcome)
            if item
        ).lower()
        if any(hint in searchable for hint in UNSAFE_HINTS):
            return "unsafe_mutation"
        if action.type in {"press", "click"}:
            return "safe_local_edit"
        return "unknown_risk"

    def requires_confirmation(self, action: BrowserAction, observation: BrowserObservation | None = None) -> bool:
        return self.classify(action, observation) in {"unsafe_mutation", "unknown_risk"}

    def _looks_like_date_field(self, action: BrowserAction) -> bool:
        searchable = " ".join(
            item
            for item in (action.target_hint, action.target_id or "", action.expected_outcome)
            if item
        ).lower()
        return any(token in searchable for token in ("日期", "时间", "date"))
