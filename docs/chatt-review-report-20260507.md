# Chat Mode Review Report - 2026-05-07

## 1. 背景与目标

本次工作的起点是：`opsAgent` 原有运行方式主要是一次性 CLI 参数输入，例如：

```bash
aiops-agent run "巡检生产环境 WebLogic"
```

这种方式适合执行单个明确任务，但不适合持续交互。用户希望增加一种 chat 方式运行，让运维工程师可以在同一个终端会话中连续输入自然语言，与 agent 实时交互，并在执行过程中看到阶段反馈。

本次目标可以拆成三层：

1. 新增 `aiops-agent chat` 终端 REPL。
2. 让 chat 复用现有 `AgentController`、session、task、confirm 恢复机制。
3. 修复 chat 真实运行中暴露出的体验问题，让普通对话、运维任务和当前日期类问题都有合理行为。

最终实现后，用户可以这样启动：

```bash
python3 -m aiops_agent chat \
  --config configs/rpa.json \
  --llm-config configs/llm.json
```

进入后可以连续输入：

```text
opsAgent> hello
opsAgent> 今天几号
opsAgent> 巡检生产环境 WebLogic
opsAgent> /session
opsAgent> /new
opsAgent> /exit
```

## 2. 做了什么

### 2.1 新增 `chat` CLI 子命令

新增了 `aiops-agent chat` 命令，支持和 `run` 基本一致的运行参数：

- `--config`
- `--llm-config`
- `--credential-config`
- `--credential-ref`
- `--browser-site`
- `--browser-sites-config`
- `--session-id`
- `--llm-profile`
- `--max-steps`
- `--allowed-domains`
- `--headed`
- `--browser-trace`
- `--browser-video`
- `--browser-channel`
- `--browser-slow-mo`
- `--require-confirmation`

这样 chat 模式不是一套独立实现，而是和已有 `run` 模式共享配置入口、LLM provider、浏览器能力、凭据配置、站点配置和任务执行链路。

### 2.2 新增 `ChatRunner`

新增 `src/aiops_agent/chat.py`，核心类是：

- `ChatOptions`
- `ChatRunner`

`ChatRunner` 负责终端交互循环：

1. 打印 chat 启动提示。
2. 读取用户输入。
3. 处理内置命令。
4. 调用 `controller.run(...)` 执行自然语言任务。
5. 输出阶段反馈、任务报告、任务 ID、执行状态和会话 ID。
6. 如果任务进入 `awaiting_confirmation`，在 chat 内询问是否继续确认。

内置命令包括：

- `/exit`：退出 chat。
- `/quit`：退出 chat。
- `/session`：查看当前 session。
- `/new`：清空当前 session id，下一条任务创建新 session。

### 2.3 复用现有 session 和 task 体系

chat 没有新增独立的对话存储，而是复用了现有：

- `FileSessionStore`
- `FileTaskStore`
- `AgentSession`
- `AgentTaskState`

每一轮用户输入仍然会创建一个标准 task。chat 只是在多轮输入之间持有当前 `session_id`，并把它传给 `AgentController.run()`。

这样做的好处是：

- chat 产生的任务仍然可以被现有审计和持久化系统追踪。
- `session list`、`session close` 仍然适用。
- Browser Agent 的登录态、上下文压缩和最近 observation 仍然按原机制工作。
- 不需要引入新的 conversation 数据模型。

### 2.4 增加阶段级实时反馈

新增 `src/aiops_agent/agent/progress.py`：

```python
ProgressEvent(
    stage: str,
    message: str,
    task_id: str | None = None,
    session_id: str | None = None,
    details: dict = {},
)
```

`AgentController.run()` 和 `AgentController.confirm()` 新增可选参数：

```python
progress_callback: Callable[[ProgressEvent], None] | None = None
```

在关键节点触发进度事件：

- `session.created`
- `session.resumed`
- `task.created`
- `intent.parsed`
- `plan.generated`
- `policy.checked`
- `tool.running`
- `summary.ready`
- `task.completed`

chat 模式会把这些事件打印为：

```text
[intent.parsed] 已识别意图：inspection。
[plan.generated] 已生成计划，工具：inspection。
[policy.checked] 策略检查通过，风险等级：read_only。
[tool.running] 正在执行工具。
[summary.ready] 已生成执行摘要。
[task.completed] 任务已结束，状态：success。
```

原有 `run` 命令默认不传 `progress_callback`，因此不会额外输出这些阶段反馈，保持兼容。

### 2.5 在 chat 内支持确认恢复

Browser Agent 原先已经支持：

```bash
aiops-agent confirm <task_id>
```

chat 模式把这个能力内联到当前对话中。

当某轮任务返回 `awaiting_confirmation` 时，chat 会展示：

- 任务 ID
- 风险等级
- 当前页面
- 待执行动作
- 目标元素
- 预期结果

然后提示：

```text
确认继续执行? [y/N]
```

用户输入：

- `y` 或 `yes`：调用 `controller.confirm(task.id)` 继续执行。
- `n`、`no` 或直接回车：跳过确认，任务保持等待确认状态。

这让浏览器自动化里的高风险动作不需要退出 chat 再手动复制 task id 执行 confirm。

## 3. 遇到的问题与解决过程

### 3.1 问题一：chat 入口实现后，用户无法判断 agent 正在做什么

#### 现象

第一版如果只是简单循环调用：

```python
controller.run(user_input, session_id=session_id)
```

用户只能等最终 report。对于浏览器任务、LLM 意图识别、策略阻断、确认等待等场景，中间过程不可见。

#### 根因

`AgentController` 原先是面向单次 CLI 的同步调用模型。内部虽然有 LangGraph 节点和 audit event，但没有对外暴露实时进度事件。

audit event 更适合持久化和排查，不适合作为用户交互输出：

- 文案偏工程化。
- 字段包含较多内部细节。
- 需要 tail 文件或额外读取。
- 不一定和当前终端交互节奏同步。

#### 解决

在 controller 层增加 `ProgressEvent` 和可选 callback，不改变原有返回值。

这样做的关键点是：

- `run` 不传 callback 时行为完全不变。
- chat 传 callback 时可以得到阶段级反馈。
- 事件来自 controller 的真实节点，不是 chat runner 自己猜状态。
- 后续如果要做 WebSocket/SSE，也可以复用同一事件模型。

### 3.2 问题二：输入 `hello` 后总是变成 `ops_qa`

#### 现象

用户启动 chat 后输入：

```text
opsAgent> hello
```

系统返回：

```text
任务类型：ops_qa
执行状态：success
执行计划：记录问答请求；调用知识库工具检查 vault 配置；返回当前阶段能力说明
```

这看起来像“opsAgent 不论收到什么输入都只会运行 ops_qa”。

#### 第一层根因：LLM 请求失败时静默 fallback

第一次排查日志时发现：

```text
HTTP/1.1 401 Authorization Required
```

也就是 LLM 请求失败。`IntentParser` 原先逻辑是：

```python
try:
    parsed = llm_provider.classify_intent(...)
except LLMError:
    return None
```

然后进入规则解析。规则解析对无法匹配的文本默认返回：

```python
IntentResult(intent="ops_qa", ...)
```

因此当 LLM key 配错或接口失败时，系统不会提示用户“LLM 没生效”，而是静默退回规则，并把 `hello` 归为 `ops_qa`。

#### 第一层解决：让 fallback 可见

为 `IntentParser` 增加：

- `last_llm_error`
- `llm_fallback_used`
- `llm_fallback_error`

当 LLM 失败后，规则解析结果会附带：

```json
{
  "llm_fallback_used": true,
  "llm_fallback_error": "..."
}
```

chat 进度输出也会提示：

```text
[intent.parsed] 已识别意图：ops_qa。 LLM 识别失败，已使用规则 fallback。
```

这样用户能明确区分：

- LLM 成功判断为 `ops_qa`。
- LLM 失败后规则 fallback 为 `ops_qa`。

#### 第二层根因：系统没有普通聊天 intent

用户修复 LLM API key 后，DeepSeek 请求成功：

```text
HTTP/1.1 200 OK
LangChain model invocation succeeded | model=deepseek-chat, role=intent
```

但 `hello` 仍然被分类为 `ops_qa`。

这不是 DeepSeek 调用失败，而是现有意图 schema 只有四类：

- `inspection`
- `permission_change`
- `ops_qa`
- `web_action`

没有 `general_chat`。模型面对 `hello` 这类非运维任务输入，只能在四个选项里选一个最接近的。`ops_qa` 是最宽泛的一类，所以它会被选中。

#### 第二层解决：新增 `general_chat`

新增 intent：

```text
general_chat
```

并调整 LLM intent prompt：

```text
Schema: {"intent": "inspection|permission_change|ops_qa|web_action|general_chat", "entities": {...}}
Use general_chat for greetings, small talk, or requests that are not enterprise ops tasks.
```

同时调整规则解析：

- `hello`
- `hi`
- `你好`
- `您好`
- `hey`
- 其他无法匹配运维关键词的文本

默认进入 `general_chat`，不再默认进入 `ops_qa`。

### 3.3 问题三：`general_chat` 仍然需要一个真实回复工具

#### 现象

新增 `general_chat` 后，如果 planning 层没有对应 tool call，任务会落入未知或占位路径，仍然不像真正聊天。

#### 根因

opsAgent 的执行链路是：

```text
intent_parse -> task_plan -> policy_check -> tool_execute -> summarize
```

也就是说 intent 只是分类。真正输出结果必须由 planning 生成 tool call，再由 tool executor 执行。

原先有：

- `inspection` -> `inspection` tool
- `ops_qa` -> `knowledge` tool
- `web_action` -> `browser_agent` tool
- `permission_change` -> 等待确认/阻断

但没有：

- `general_chat` -> chat reply tool

#### 解决

新增 `src/aiops_agent/tools/chat.py`：

```python
class ChatTool(BaseTool):
    def execute(self, params: dict) -> ToolExecutionResult:
        ...
```

`PlanningService` 新增 `general_chat` 分支：

```python
ExecutionPlan(
    selected_tools=["chat"],
    tool_calls=[
        ToolCallSpec(
            tool_name="chat",
            action="reply",
            params={"message": entities.get("raw_text", task_input)},
            risk_level="read_only",
        )
    ],
)
```

`create_controller()` 注册：

```python
registry.register(
    "chat",
    ChatTool(provider),
    risk_level="read_only",
    description="Reply to non-task interactive chat messages",
    tags=["chat", "interactive"],
)
```

`ResultSummarizer` 对 `general_chat` 做特殊处理：直接返回 `data["reply"]`，不再套用运维任务报告模板。

这样普通聊天输出不再是：

```text
任务类型：ops_qa
执行状态：success
...
```

而是直接输出自然语言回复。

### 3.4 问题四：询问日期时模型回答错误

#### 现象

用户输入：

```text
opsAgent> 今天几号
```

模型先回答无法获取当前日期。用户再输入：

```text
opsAgent> 查询今天的日期
```

模型返回：

```text
今天的日期是2025年4月11日。
```

但实际运行环境日期是：

```text
2026-05-07
```

#### 根因

这不是 DeepSeek API 本身不能回答简单问题，而是 opsAgent 没有把运行时日期传给模型。

LLM 不天然知道调用方机器上的当前日期。除非应用层注入当前时间，否则模型只能：

- 基于训练数据猜。
- 基于对话上下文猜。
- 拒绝回答。
- hallucinate 一个日期。

之前 `ChatTool` 调用：

```python
self.llm_provider.generate_chat_reply(message)
```

没有传任何 runtime context。

虽然终端日志里有真实时间：

```text
2026-05-07 22:04:06
```

但这些日志不是 prompt 的一部分，模型看不到。

#### 解决

`ChatTool` 新增运行时上下文：

```python
{
  "current_datetime": "...",
  "current_date": "...",
  "timezone": "Asia/Shanghai"
}
```

默认时区为：

```text
Asia/Shanghai
```

调用 LLM 时改为：

```python
reply = self.llm_provider.generate_chat_reply(message, context=context)
```

同时强化 system prompt：

```text
Runtime context is authoritative. For questions about today, current date,
current time, or relative dates, use only the provided runtime context and do not guess.
```

这样模型在回答“今天几号”“现在几点”“明天是哪天”这类问题时，有明确、权威的运行时依据。

### 3.5 问题五：LLM API key 暴露风险

#### 现象

用户在贴运行日志和配置时暴露了完整 API key。

#### 风险

任何已经暴露在对话、日志、截图或提交记录中的 key，都应该视为泄漏。即使只是本地调试，也可能进入：

- shell history
- chat history
- terminal log
- git diff
- issue/PR 评论
- 截图

#### 处理建议

本次没有在报告里复述 key。建议立即在 DeepSeek 控制台轮换该 key，并避免把 `configs/llm.json` 中的真实 key 提交到 git。

后续应该补充：

- `.gitignore` 覆盖本地真实 LLM 配置。
- 提供 `configs/llm.example.json`。
- 支持从环境变量读取 key，并推荐生产用法。
- 启动时对日志输出做 secret redaction 检查。

## 4. 当前实现效果

### 4.1 普通聊天

输入：

```text
opsAgent> hello
```

预期流程：

```text
[intent.parsed] 已识别意图：general_chat。
[plan.generated] 已生成计划，工具：chat。
[policy.checked] 策略检查通过，风险等级：read_only。
[tool.running] 正在执行工具。
[summary.ready] 已生成执行摘要。
[task.completed] 任务已结束，状态：success。
```

最终输出是自然语言回复，而不是 `ops_qa` 的知识库报告。

### 4.2 日期问题

输入：

```text
opsAgent> 今天几号
```

`ChatTool` 会向 LLM 注入：

```json
{
  "current_datetime": "2026-05-07T...",
  "current_date": "2026-05-07",
  "timezone": "Asia/Shanghai"
}
```

预期回答应基于该上下文，而不是模型猜测。

### 4.3 运维问答

输入：

```text
opsAgent> 如何处理 WebLogic 连接池告警
```

仍然应该进入：

```text
ops_qa -> knowledge
```

这是运维知识问答，不应该被普通聊天吞掉。

### 4.4 巡检任务

输入：

```text
opsAgent> 巡检生产环境 WebLogic
```

仍然应该进入：

```text
inspection -> inspection tool
```

### 4.5 浏览器任务

输入：

```text
opsAgent> 打开 http://localhost:3000 并总结页面
```

或结合配置：

```bash
python3 -m aiops_agent chat \
  --config configs/rpa.json \
  --llm-config configs/llm.json \
  --browser-sites-config configs/browser_sites.json \
  --browser-site mock-admin \
  --credential-config configs/credentials.json \
  --credential-ref admin
```

仍然走：

```text
web_action -> browser_agent
```

遇到远端提交动作时，在 chat 内确认。

## 5. 测试与验证

本次新增和调整了测试覆盖。

### 5.1 ChatRunner 测试

新增 `tests/test_chat.py`，覆盖：

- `chat` 参数解析。
- 普通任务执行后输出阶段反馈。
- 连续两轮任务复用同一个 session。
- `/new` 后下一条任务创建新 session。
- `/exit` 正常退出。
- `awaiting_confirmation` 后输入 `n` 不恢复执行。
- `awaiting_confirmation` 后输入 `yes` 调用 `confirm()`。

### 5.2 IntentParser 测试

扩展 `tests/test_intent_parser.py`，覆盖：

- `hello` 规则解析为 `general_chat`。
- LLM 成功返回 `general_chat` 时保留 LLM metadata。
- LLM 失败时 fallback 到规则解析，并记录：
  - `llm_fallback_used`
  - `llm_fallback_error`
- 原有 inspection、permission_change、ops_qa、web_action 解析不回退。

### 5.3 Agent flow 测试

扩展 `tests/test_agent_flow.py`，覆盖：

- `general_chat` 会进入 `chat` tool。
- `ChatTool` 会调用 `generate_chat_reply()`。
- `ChatTool` 会传入 runtime context。
- runtime context 包含：
  - `current_date`
  - `timezone == "Asia/Shanghai"`
- 最终 `task.report` 直接等于聊天回复，不再套用任务报告模板。

### 5.4 全量测试结果

当前全量测试通过：

```text
38 passed
```

## 6. 关键设计取舍

### 6.1 为什么不让 chat 绕过 AgentController

可以让 `ChatRunner` 直接调用 LLM 来实现普通聊天，但这样会绕过：

- task 创建
- session 记录
- audit
- progress event
- policy
- summarizer

本次选择让 `general_chat` 也走标准 agent pipeline。代价是多一次 intent 识别和 tool 执行，但好处是所有交互都可追踪、可测试、可审计。

### 6.2 为什么新增 `general_chat` 而不是把 `ops_qa` 改宽

`ops_qa` 在 opsAgent 中代表运维知识问答，后续会接 Obsidian、SOP、故障知识库、检索和引用来源。

如果把普通聊天也塞进 `ops_qa`，会造成几个问题：

- `hello` 会触发知识库工具。
- 非任务闲聊会污染运维问答统计。
- 后续知识库质量评估会被闲聊噪声干扰。
- 用户无法区分“普通对话”和“运维知识问答”。

因此新增 `general_chat` 是更清晰的边界。

### 6.3 为什么阶段反馈先做到 controller 级别

第一版只输出：

- intent
- plan
- policy
- tool
- summary
- completed

没有输出 Browser Agent 每一个 click/type/observe 动作。

原因是 controller 级事件稳定，改动小，适合作为通用 chat 反馈层。浏览器步骤级反馈更细，但需要把 Browser Agent 的内部 audit event 或 action loop 也接入 progress callback，这会扩大改动面。

后续可以做第二阶段增强。

### 6.4 为什么日期通过 runtime context 注入，而不是让模型自己回答

模型回答“今天几号”必须依赖调用时刻。这个信息不在模型参数里，也不一定在训练数据里。

正确做法是应用层注入：

- 当前日期
- 当前时间
- 时区

并在 prompt 中明确说明 runtime context 是权威来源。

这比“相信模型知道今天”可靠得多，也方便后续做时间相关工具调用。

## 7. 当前限制

### 7.1 chat 还不是完整 assistant runtime

现在的 chat 是终端 REPL，不是 Web API、WebSocket 或 SSE 服务。

它适合本地开发、演示和运维工程师命令行使用，但还不能直接作为前端页面的实时会话后端。

### 7.2 多轮上下文仍然比较弱

虽然 chat 复用了 session，但普通聊天 LLM prompt 目前只注入 runtime context，没有把 session summary、上一轮问答、最近 task report 等完整注入给 chat reply。

因此它能连续执行任务，但普通聊天的多轮记忆还不强。

### 7.3 日期上下文只覆盖 chat tool

当前 runtime context 注入在 `ChatTool` 中。

如果后续 LLM planner、ops_qa、browser planner 也需要“今天”“本周”“昨天”这类时间理解，需要把 runtime context 提升到更通用的 controller 或 planning context。

### 7.4 fallback 仍然是规则解析

LLM 失败后系统仍会 fallback 到规则解析，这是为了保持可用性。

但对于 chat 模式，用户可能更希望看到明确错误，而不是 fallback 后继续执行一个保守意图。当前已经显示 fallback 提示，但还没有提供“禁用 fallback”或“LLM 失败即中断”的选项。

### 7.5 日志噪声较多

chat 输出中仍会混入：

- `aiops_agent.cli` INFO 日志
- `httpx` 请求日志
- `langchain_openai` proxy 提示

这些对开发有用，但对普通终端 chat 体验偏吵。后续需要区分：

- 交互输出
- 运行日志
- debug 日志

### 7.6 secret 管理还需要加强

`configs/llm.json` 当前容易被用户直接填真实 key。

后续需要通过 example config、环境变量、`.gitignore` 和 secret redaction 降低泄漏风险。

## 8. 下一步建议

### 8.1 改善 chat 日志体验

建议将 chat 默认日志等级降到 `WARNING`，或将运行日志输出到 stderr/文件，只在 stdout 显示交互内容。

目标效果：

```text
opsAgent> hello
[intent.parsed] 已识别意图：general_chat。
你好，我是 opsAgent。
```

而不是夹杂大量 HTTP 和 SDK 日志。

### 8.2 增加 `/help` 命令

当前启动提示只列出 `/exit`、`/session`、`/new`。

建议新增：

```text
/help
```

输出：

- 可用内置命令。
- 示例任务。
- 当前 session。
- 当前配置摘要。
- 如何确认高风险动作。

### 8.3 增加 `/debug on|off`

用户调试 LLM、browser、policy 时需要日志；普通使用时不需要。

建议支持：

```text
/debug on
/debug off
```

或者启动参数：

```bash
--quiet
--verbose
```

### 8.4 把 session summary 注入普通聊天

`ChatTool` 后续可以接收：

- 当前 session id
- session summary
- rolling summary
- last task id
- last task status
- last report 摘要

这样用户可以问：

```text
刚才那个任务结果是什么？
继续上一步
这个会话里做过哪些操作？
```

### 8.5 把 runtime context 提升为通用上下文

当前 runtime context 只给 `general_chat`。

建议后续在 controller 层统一注入：

```json
{
  "current_datetime": "...",
  "current_date": "...",
  "timezone": "...",
  "trace_id": "...",
  "session_id": "..."
}
```

然后传给：

- intent parser
- planning service
- chat tool
- knowledge tool
- browser planner

这样“今天”“昨天”“本周”“最近一次任务”等表达能在所有能力中一致处理。

### 8.6 增加真正的运维工具型时间查询

普通聊天可以回答日期，但从 agent 设计看，更好的做法是新增一个只读 system tool：

```text
system_info
```

支持：

- 当前日期
- 当前时间
- 时区
- hostname
- 当前用户
- 当前工作目录
- 环境检查

这样用户输入“今天几号”可以选择：

```text
general_chat -> chat
```

或：

```text
system_query -> system_info
```

第二种更可审计，也更适合严肃运维场景。

### 8.7 强化 LLM 分类边界

当前 `general_chat` 加入后解决了闲聊误入 `ops_qa` 的问题，但还需要更多边界测试：

- “帮我看看 WebLogic” 应该是 inspection 还是 ops_qa？
- “打开公司后台” 应该是 web_action，但可能缺 URL/site。
- “给张三授权，但先别执行” 应该是 permission_change 还是 planning-only？
- “今天巡检生产环境 WebLogic” 应该识别日期修饰和 inspection。

建议补一组高质量 intent golden tests。

### 8.8 增加配置样例与 secret 防护

建议新增：

```text
configs/llm.example.json
configs/credentials.example.json
```

并确保真实配置不会被提交：

```gitignore
configs/llm.local.json
configs/credentials.local.json
```

启动文档中推荐：

```bash
AIOPS_LLM_API_KEY=... python3 -m aiops_agent chat ...
```

而不是把 key 写入仓库内 JSON。

## 9. 总结

这次 chat 改造把 opsAgent 从“一次性自然语言任务执行器”推进到“可连续交互的终端 agent”。

核心成果包括：

- 新增 `aiops-agent chat`。
- 支持多轮 session 复用。
- 支持 `/exit`、`/quit`、`/session`、`/new`。
- 支持阶段级实时反馈。
- 支持 chat 内人工确认。
- 修复 LLM fallback 不可见的问题。
- 新增 `general_chat`，避免闲聊误入 `ops_qa`。
- 新增 `ChatTool`，让普通聊天走标准 agent pipeline。
- 给 chat LLM 注入当前日期、时间和时区，修复日期 hallucination。
- 保持原有 `run`、`confirm`、`session` 兼容。
- 全量测试通过。

当前版本已经能满足本地终端交互的第一阶段目标。下一阶段重点应该放在日志体验、多轮上下文、时间/系统信息工具、secret 管理和更严格的 intent 边界测试上。
