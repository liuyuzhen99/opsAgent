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
        if action.type in {"type_username", "type_password", "login_submit"}:
            return "safe_local_edit"
        # Some command buttons only open a confirmation or preview UI. The
        # irreversible boundary is the subsequent confirm/submit action, so
        # prompting on both clicks creates two confirmations for one mutation.
        if action.type == "click" and self.opens_confirmation_ui(action):
            return "safe_local_edit"
        if action.risk_level in {"unsafe_mutation", "unknown_risk"}:
            return action.risk_level
        if action.type in {"open_url", "observe_page", "wait_for", "extract_text", "save_artifact", "finish"}:
            return "safe_read"
        if action.type == "hover":
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
        direct_target = " ".join(item for item in (action.target_hint, action.value or "") if item).lower()
        if action.type == "click" and self._is_review_mutation_target(direct_target):
            return "unsafe_mutation"
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

    def _is_review_mutation_target(self, target: str) -> bool:
        compact = "".join(target.split()).strip("：:，,。.!！")
        return compact in {"复核", "审核", "approve"} or any(
            marker in compact for marker in ("确认复核", "复核按钮", "确认审核", "审核按钮")
        )

    def opens_confirmation_ui(self, action: BrowserAction) -> bool:
        outcome = "".join((action.expected_outcome or "").lower().split())
        if not outcome:
            return False
        ui_markers = (
            "确认对话框",
            "确认弹窗",
            "确认窗口",
            "确认页面",
            "确认界面",
            "确认框",
            "预览页面",
            "预览界面",
            "previewpage",
            "previewdialog",
            "confirmationdialog",
            "confirmdialog",
            "confirmationmodal",
            "confirmmodal",
        )
        open_markers = ("打开", "弹出", "显示", "进入", "open", "show", "display", "launch")
        non_committing_markers = (
            "仅打开",
            "只打开",
            "仅弹出",
            "只弹出",
            "不提交",
            "不会提交",
            "withoutsubmitting",
            "doesnotsubmit",
            "onlyopen",
            "onlyshow",
        )
        speculative_markers = ("可能", "maybe", "might", "mayopen", "mayshow")
        return (
            any(marker in outcome for marker in ui_markers)
            and any(marker in outcome for marker in open_markers)
            and any(marker in outcome for marker in non_committing_markers)
            and not any(marker in outcome for marker in speculative_markers)
        )
