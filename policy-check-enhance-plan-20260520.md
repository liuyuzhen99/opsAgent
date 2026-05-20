# Policy Check 通用确认恢复改造计划

## Summary

将外层 `policy_check` 从“只标记 `awaiting_confirmation`”升级为可恢复的计划级确认检查点。保留现有 LangGraph 主流程和 browser action 级确认机制，同时让 `permission_change`、`--require-confirmation` 等非 browser 场景能被 chat/CLI `confirm` 正确处理。

确认后若没有真实可执行工具，任务转为 `blocked`，并明确记录“用户已确认，但当前能力未接入执行工具”。这符合当前 `permission_change` 只完成治理确认、尚未接入真实权限变更工具的阶段边界。

## Scope

- 新增 plan-level confirmation checkpoint。
- 不改变 browser agent 内部动作级确认恢复协议。
- 不引入外部审批、RBAC、用户身份模型或企业级策略配置。
- 不重新进入 `policy_check` 执行确认后的 plan tool calls，避免 `--require-confirmation` 二次卡住。

## Data Contract

`PolicyDecision` 增加结构化 `data` 字段，用于承载策略层确认语义：

- `confirmation_type`: `"plan"`
- `resume_node`: `"tool_execute"`
- `confirmed`: `false`

`_policy_check_node` 在策略 payload 基础上补齐任务上下文：

- `status`: `"awaiting_confirmation"`
- `confirmation_summary`:
  - `prepared_action`
  - `target`
  - `expected_outcome`
  - `current_page`
  - `current_url`
- `pending_tool_calls`: `task.tool_calls` 的可序列化 dict 列表；无工具时为空列表。
- `intent`、`entities`、`plan_steps`、`confirmation`: 保留供摘要、chat 和审计展示使用。

确认后无工具的 blocked result 使用稳定原因码：

- `data.status`: `"blocked"`
- `data.block_reason`: `"confirmed_without_executable_tool"`
- `data.confirmation`: `{"type": "plan", "confirmed": true}`

## Controller Changes

`AgentController.confirm()` 调整为双路径：

1. 若存在 `pending_action_raw`，继续走现有 browser action 级恢复逻辑。
2. 否则若 `confirmation_type == "plan"`，走计划级恢复：
   - 记录 `confirmation.confirmed` 审计事件。
   - 将确认上下文标记为 `confirmed=True`。
   - 反序列化 `pending_tool_calls` 为 `ToolCallSpec`。
   - 若存在工具调用，直接执行工具、汇总、持久化、压缩 session。
   - 若不存在工具调用，`mark_blocked()` 并输出“已确认，但当前任务没有可执行工具”。

为避免 confirm 路径和 graph 工具执行路径继续分叉，抽取内部 helper：

- 反序列化 pending tool calls。
- 执行单个 tool call，并复用现有状态映射、artifact 收集、`tool_called` 审计和 skill fallback。
- 完成确认后的总结、task/session 持久化、`task.completed` 与 `task_completed` 审计事件。

## Browser Compatibility

- browser action 级确认仍以 `pending_action_raw` 为最高优先级。
- `replay_actions`、`session_state_path`、`completed_action_keys`、`resume_url` 等字段保持原恢复逻辑。
- `PolicyEngine` 仍不在外层拦截 `web_action` 的动作级风险。

## User-Facing Output

- `ResultSummarizer` 对 `awaiting_confirmation` 继续提示人工确认。
- 对 `block_reason == "confirmed_without_executable_tool"` 输出明确建议：当前只完成治理确认，需接入权限变更或目标执行工具后才能执行。
- chat 的确认展示继续读取 `confirmation_summary`，plan-level 和 browser-level 使用同一展示字段。

## Test Plan

- `permission_change` 首次 run 进入 `awaiting_confirmation`，result data 包含：
  - `confirmation_type == "plan"`
  - `confirmation_summary`
  - `pending_tool_calls == []`
- 对该 task 调 `confirm()` 后状态为 `blocked`：
  - result data 包含 `block_reason == "confirmed_without_executable_tool"`
  - 审计包含 `confirmation.confirmed` 和完成事件。
- `--require-confirmation` 作用在有工具的 read-only 任务时：
  - 首次进入 `awaiting_confirmation`
  - confirm 后恢复执行工具并成功
- 保持现有 browser resume tests：
  - `test_controller_confirm_resumes_pending_browser_action`
  - browser workflow 多次 remote mutation 确认行为不变
- Chat 行为：
  - plan-level awaiting_confirmation 仍能展示确认提示
  - 用户输入 `y` 后，无工具打印 blocked 报告；有工具继续执行
- 回归：
  - 运行 `python3 -m unittest`

## Implementation Summary

已按本计划完成实施。

### Files Changed

- `policy-check-enhance-plan-20260520.md`
  - 新增本计划文档，并在实施后追加执行记录。
- `src/aiops_agent/tasks/models.py`
  - `PolicyDecision` 增加 `data: dict[str, Any]` 字段，用于承载策略层结构化确认 payload。
- `src/aiops_agent/policy.py`
  - `PolicyEngine.evaluate()` 对需要确认且非 `web_action` 的任务返回 plan-level confirmation payload：
    - `confirmation_type="plan"`
    - `resume_node="tool_execute"`
    - `confirmed=False`
- `src/aiops_agent/agent/controller.py`
  - `confirm()` 改为双路径分流：
    - 若存在 `pending_action_raw`，优先走 browser action 级恢复。
    - 否则若 `confirmation_type == "plan"`，走 plan-level confirmation 恢复。
  - 新增 `_confirm_browser_action()`，保留原 browser 恢复字段：
    - `confirmed_action`
    - `replay_actions`
    - `prior_steps`
    - `completed_action_keys`
    - `session_state_path`
    - `resume_url`
  - 新增 `_confirm_plan()`：
    - 记录 `confirmation.confirmed` 审计事件。
    - 标记确认上下文 `confirmed=True`。
    - 反序列化 `pending_tool_calls`。
    - 有工具时直接恢复执行工具。
    - 无工具时 `mark_blocked()`，并写入 `block_reason="confirmed_without_executable_tool"`。
  - 新增 `_deserialize_tool_calls()`，将 `result.data.pending_tool_calls` 中的 dict 恢复成 `ToolCallSpec`。
  - 新增 `_execute_confirmed_tool_call()`，复用确认后工具执行逻辑：
    - 执行工具。
    - 收集 artifacts。
    - 记录 `tool_called` 审计。
    - 保留 skill fallback。
    - 按 `success`、`awaiting_confirmation`、`blocked`、失败状态映射任务状态。
  - 新增 `_finalize_confirmed_task()`，统一确认后收尾：
    - 生成摘要。
    - 压缩并保存 session。
    - 持久化 task。
    - 记录 `memory.compressed`、`task.completed`、`task_completed` 审计事件。
  - `_policy_check_node()` 对 `awaiting_confirmation` 使用 `_build_plan_confirmation_payload()` 写入统一确认 payload。
  - 新增 `_build_plan_confirmation_payload()`：
    - 写入 `status="awaiting_confirmation"`。
    - 写入 `confirmation_summary`，兼容 chat 当前展示字段。
    - 写入 `pending_tool_calls` 的可序列化版本。
    - 保留 `intent`、`entities`、`plan_steps`。
- `src/aiops_agent/agent/summarizer.py`
  - 对 `blocked` 且 `block_reason == "confirmed_without_executable_tool"` 的结果输出明确建议：
    - 用户已确认治理计划。
    - 当前任务没有可执行工具。
    - 需要接入权限变更或目标执行工具后才能执行。
- `tests/test_agent_flow.py`
  - 更新 `permission_change` 首次 run 断言：
    - `status == "awaiting_confirmation"`
    - `confirmation_type == "plan"`
    - 存在 `confirmation_summary`
    - `pending_tool_calls == []`
  - 新增 `test_permission_change_confirm_without_tool_becomes_blocked`：
    - plan-level confirm 后任务进入 `blocked`。
    - result data 包含 `block_reason="confirmed_without_executable_tool"`。
    - 审计包含 `confirmation.confirmed` 和完成事件。
  - 新增 `test_require_confirmation_resumes_tool_execution_after_confirm`：
    - `--require-confirmation` 作用在 inspection read-only 工具任务。
    - 首次 run 进入 plan-level `awaiting_confirmation`。
    - confirm 后恢复执行 inspection 工具并成功。
- `pyproject.toml`
  - 补充缺失依赖声明 `langchain-text-splitters>=1.0.0`。
- `src/aiops_agent.egg-info/requires.txt`
  - 同步补充 `langchain-text-splitters>=1.0.0`。

### Behavior After Implementation

- `permission_change`
  - 首次 run 会进入 `awaiting_confirmation`。
  - 返回可展示、可恢复的 plan-level confirmation payload。
  - confirm 后由于当前没有权限变更执行工具，任务进入 `blocked`。
  - 报告明确说明“用户已确认，但当前任务没有可执行工具”。

- `--require-confirmation`
  - 对非 browser 且有真实工具的任务，首次 run 会进入 plan-level `awaiting_confirmation`。
  - confirm 后不重新经过 `policy_check`，直接恢复执行 `pending_tool_calls`，避免二次确认死循环。

- browser action 级确认
  - `pending_action_raw` 仍是最高优先级。
  - 原有逐动作确认恢复协议保持不变。
  - `web_action` 的动作级风险仍由 browser agent 内部处理，外层 `PolicyEngine` 不拦截。

- chat/CLI
  - chat 继续读取 `confirmation_summary` 展示确认提示。
  - 用户输入 `y` 后：
    - 无工具场景输出 blocked 报告。
    - 有工具场景继续执行。
  - CLI `confirm` 可恢复 plan-level confirmation。

### Verification

已运行全量回归测试：

```bash
python3 -m pytest -q
```

结果：

```text
159 passed, 6 skipped, 1 warning
```

另外单独确认过：

- `tests/test_agent_flow.py`
- `tests/test_phase2_resume.py::test_controller_confirm_resumes_pending_browser_action`
- `tests/test_chat.py`

本机测试环境中为运行完整测试安装了缺失依赖：

- `langchain-text-splitters`
- `rank-bm25`

其中 `rank-bm25` 已在项目依赖中声明；`langchain-text-splitters` 本次已补充到依赖声明。
