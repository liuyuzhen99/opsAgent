# Phase 2 Summary

## 当前结论

Phase 2 的核心目标已经完成：Browser Agent 不再依赖固定动作脚本，而是可以基于页面 observation 动态规划只读网页任务，并在需要登录时完成最小账号密码闭环。系统现在支持：

- 识别 `web_action` 意图并生成结构化浏览器任务。
- 使用 Playwright 打开网页、观察页面、点击、输入、提交登录表单、抽取文本并保存 artifact。
- 在远程副作用动作前阻断，等待人工确认后再恢复执行。
- 通过本地凭据配置加载用户名和密码，任务和审计日志只保存 `credential_ref`，不保存明文凭据。
- 对 session 做最小持久化，支持浏览器 `storage_state` 复用、rolling summary、最近 observations 和任务恢复。
- 提供 Obsidian vault 的配置与工具契约，为后续知识库问答接入留出边界。

当前实现已经覆盖 Phase 2 文档中的 P0/P1 主体能力，并补上了登录与凭据的 P0/P1 缺口。没有实现的部分主要是有意延期的增强项，例如真实 Obsidian RAG、长期用户画像、多标签页复杂编排、验证码/MFA 自动处理和任意浏览器脚本执行平台。

## 已完成能力

### P0: Browser Agent 最小闭环

- 新增 `web_action` intent 解析，规划层会把网页任务转换成 `browser_agent` tool call。
- 新增 `BrowserTaskSpec`、`BrowserObservation`、`BrowserAction` 等结构化模型。
- 新增 `BrowserPlanner`，根据当前 observation 和已执行 steps 动态决定下一步，而不是只跑预置动作列表。
- 新增 `BrowserAgentTool`，负责执行循环、失败阈值、重复动作阈值、artifact 输出、审计事件和最终报告。
- 新增 `PlaywrightBrowserTool`，封装 open/observe/click/type/select/press/wait/extract/save/finish/login_submit 等动作。
- 只读任务可以完成 “打开页面 -> 观察 -> 抽取 -> 保存 -> 结束” 的完整流程。

### P0/P1: 风险控制与人工确认

- 新增浏览器动作风险评估：
  - `safe_read`: 打开页面、观察页面、抽取内容、保存 artifact、结束任务。
  - `safe_local_edit`: 本地输入、选择、按键等不会提交远程变更的动作。
  - `unsafe_mutation`: 远程提交、购买、删除、发送、发布等动作。
  - `unknown_risk`: 无法可靠判断的动作。
- 对远程副作用动作默认阻断，写入 pending action，任务进入等待确认状态。
- CLI 新增 `confirm <task_id>`，确认后从任务和 session 状态恢复，继续执行被阻断动作。
- 登录提交被作为必要认证动作处理，但登录后的业务写操作仍然需要确认。

### P0/P1: 登录与凭据最小闭环

- 新增 `--credential-config` 和 `--credential-ref`。
- 新增本地 JSON 凭据配置加载器，只支持通过引用读取凭据。
- 任务、session、审计和 artifact 中只保留 `credential_ref`，不持久化 username/password 明文。
- Planner 在 `requires_login=True` 且 observation 为登录页时生成：
  - `type_username`
  - `type_password`
  - `login_submit`
- 页面出现 MFA 或验证码时直接 `blocked`，不尝试绕过。
- Playwright observation 增强了 password 字段、登录按钮、错误提示、MFA/验证码页面的识别。
- 登录失败时保存截图和 page summary，便于排查失败原因。

### P1: Session 恢复

- session 模型新增状态、rolling summary、recent observations、metadata。
- Browser Agent 保存每个 session 的 Playwright `storage_state`。
- 后续任务使用同一 `session_id` 时可以复用登录态。
- `AgentController.confirm()` 可以加载等待确认的任务，恢复 pending action 并继续执行。
- CLI 新增 session list/close 等基础命令。

### P1/P2: 审计、artifact 和可观测性

- 浏览器执行过程会记录关键 audit event。
- 敏感字段名包含 `password`、`token`、`cookie`、`secret` 的内容会被脱敏。
- 登录动作中的用户名和密码输入值不会以明文进入审计。
- observation 会屏蔽账号、密码等输入框值。
- 支持保存页面文本、page summary、截图。
- 支持按参数启用 Playwright trace/video，便于真实浏览器排查。

### P2: Obsidian Vault 契约

- 新增 knowledge tool 的配置和工具边界。
- 当前实现是 vault 可用性检查和占位问答结果，不包含完整检索、嵌入、召回、重排或引用抽取。
- 这样做是为了先稳定规划层和工具协议，避免在 Phase 2 同时引入浏览器自动化和知识检索两条高风险主线。

## 验收情况

已完成自动化回归：

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest
25 passed in 0.26s
```

已完成真实 Chromium 验证：

- 动态只读浏览任务可执行，动作序列为：
  - `open_url`
  - `observe_page`
  - `extract_text`
  - `save_artifact`
  - `finish`
- 登录成功场景可执行，动作序列为：
  - `open_url`
  - `observe_page`
  - `type_username`
  - `type_password`
  - `login_submit`
  - `extract_text`
  - `save_artifact`
  - `finish`
- 登录态复用可执行：
  - 第一次任务登录成功并写入 session state。
  - 第二次任务使用同一 `session_id` 直接进入 dashboard。
  - 第二次任务没有再次执行登录动作。
- 明文凭据泄漏检查通过：
  - 任务、审计和 artifact 中未发现用户名或密码明文。

## 当前使用方式

只读网页任务示例：

```bash
aiops-agent run "打开本地页面并总结内容" --allowed-domains 127.0.0.1,localhost
```

带凭据登录任务示例：

```bash
aiops-agent run "登录并查看 dashboard" \
  --allowed-domains 127.0.0.1,localhost \
  --credential-config ./credentials.local.json \
  --credential-ref local_demo
```

确认远程副作用动作：

```bash
aiops-agent confirm <task_id>
```

查看和关闭 session：

```bash
aiops-agent session list
aiops-agent session close <session_id>
```

## 明确未完成或延期内容

- 不自动处理 MFA、OTP、短信验证码、扫码登录或图形验证码。
- 不绕过反自动化检测。
- 不执行任意用户提供的浏览器脚本。
- 不实现完整 Obsidian RAG，只保留配置和工具契约。
- 不接入系统钥匙串、云密钥管理或企业级密钥轮换。
- 不提供长期用户画像和跨长期任务记忆。
- 不做多标签页、多窗口或复杂异步网页工作流编排。

## 当前风险与建议

- 本地凭据文件需要由调用方控制权限，仓库不应提交真实凭据。
- Playwright `storage_state` 包含登录态，应当视为敏感文件管理。
- 现在的 planner 是规则优先的动态规划，适合 Phase 2 的安全边界；如果后续接入 LLM 页面理解，需要继续保留风险评估和确认门禁。
- 后续最值得做的增强是：
  - 增加更多真实站点的只读验收样例。
  - 为登录失败原因建立结构化错误码。
  - 把 knowledge tool 从占位契约升级为真实 Obsidian 检索。
  - 为 `storage_state` 和 artifacts 增加生命周期清理策略。
