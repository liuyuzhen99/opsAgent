from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiops_agent.llm.base import BaseLLMProvider, LLMError
from aiops_agent.tasks.models import ToolExecutionResult
from aiops_agent.tools.base import BaseTool


class ChatTool(BaseTool):
    def __init__(self, llm_provider: BaseLLMProvider | None = None, timezone: str = "Asia/Shanghai"):
        self.llm_provider = llm_provider
        self.timezone = timezone

    def execute(self, params: dict) -> ToolExecutionResult:
        message = str(params.get("message") or "")
        context = self._runtime_context()
        llm_context = dict(context)
        session_memory = params.get("session_memory")
        if isinstance(session_memory, dict):
            llm_context["session_memory"] = session_memory
        if self.llm_provider is None or not self.llm_provider.enabled:
            return ToolExecutionResult(
                success=True,
                data={
                    "message": message,
                    "reply": "你好，我是 opsAgent。你可以直接输入巡检、网页操作或运维问答任务。",
                    "llm_used": False,
                    "runtime_context": context,
                },
            )
        try:
            reply = self.llm_provider.generate_chat_reply(message, context=llm_context)
        except LLMError as exc:
            return ToolExecutionResult(
                success=False,
                error=str(exc),
                retryable=True,
                data={
                    "message": message,
                    "reply": "LLM 聊天回复失败，请检查 LLM 配置后重试。",
                    "llm_used": True,
                    "runtime_context": context,
                },
            )
        return ToolExecutionResult(
            success=True,
            data={"message": message, "reply": reply, "llm_used": True, "runtime_context": context},
        )

    def _runtime_context(self) -> dict[str, str]:
        now = datetime.now(ZoneInfo(self.timezone))
        return {
            "current_datetime": now.isoformat(timespec="seconds"),
            "current_date": now.date().isoformat(),
            "timezone": self.timezone,
        }
