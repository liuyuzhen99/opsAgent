# opsAgent LangGraph Compatible 全量实施记录 - 2026-06-02

## 文档目的

本文档整理从最初《opsAgent LangGraph 深度升级计划（最终修订版）》到当前实现状态的完整实施记录。

覆盖范围包括：

1. 最初计划的目标、范围、约束和 rollout。
2. 当前已经落地的全部实现内容。
3. 每个计划项与实际代码变更的对应关系。
4. 关键设计取舍、兼容保留和未偏离说明。
5. 测试覆盖、验证结果和剩余生产维护事项。

结论先行：

- LangGraph 深度升级计划的核心目标已经完成。
- 实施方向没有偏离最初计划。
- `ContextCompressor` 没有删除，但已经冻结为 legacy shim，运行时不再调用，属于兼容期保留，不属于方向偏离。
- 当前全量测试通过：`208 passed, 9 skipped, 1 warning`。

## 最初计划摘要

最初计划的总目标是：

将 opsAgent 升级为 LangGraph 原生运行时。

架构上：

- 主图负责任务接入、意图解析、计划生成、策略检查、路由执行、总结和审计。
- `web_action` 独立成为 WebAgentSubgraph。
- `ops_qa` / `knowledge_write` 独立成为 KnowledgeSubgraph。
- 使用 LangGraph checkpointer 管执行恢复和 interrupt resume。
- 使用 LangGraph Store 管长期 memory。
- 使用 short-term memory 加 `langmem` summarization/custom strategy 管上下文压缩。
- 不新增独立 memory subgraph。
- 不再保留 `ContextCompressor` 作为中心化 memory 框架。

最初计划明确的主图节点为：

```text
intake -> intent_parse -> task_plan -> policy_check -> route_execution -> summarize -> persist_audit
```

最初计划明确的 Web 子图节点为：

```text
prepare_spec -> load_web_memory -> restore_browser_context -> plan_action
-> stabilize_action -> risk_gate -> execute_action -> observe_page
-> reflect -> route_next -> finalize
```

最初计划明确的 Knowledge QA 分支为：

```text
validate_config -> load_knowledge_memory -> rewrite_query -> retrieve
-> emit_sources -> synthesize -> evaluate_or_skip -> finalize
```

最初计划明确的 Knowledge write 分支为：

```text
validate_config -> load_knowledge_memory -> prepare_note -> write_note
-> reindex_or_skip -> finalize
```

## 最初计划的核心约束

### Runtime And Persistence

计划要求：

- 升级 `langgraph`。
- 新增 `langgraph-checkpoint-sqlite`。
- 新增 `langmem`。
- SQLite checkpointer 默认路径为 `storage/langgraph/checkpoints.sqlite`。
- Store 默认路径为 `storage/langgraph/store/`。
- `thread_id = task.id`。
- `session_id` 放入 graph state/context。
- graph state 只保存可序列化数据。
- Playwright browser/page/context 等 live object 只能放 runtime cache。
- 现有 task/session/audit JSON 保留为兼容与审计层，不作为新 memory 主来源。

### Main Graph

计划要求：

- 固定主图节点。
- `policy_check` 中计划级人工确认改为 LangGraph `interrupt()`。
- `route_execution` 将 web/knowledge 路由到 subgraph。
- `inspection`、`rpa_action`、`general_chat` 暂时保留 ToolExecutor。
- `summarize/persist_audit` 复用现有 summarizer/task/audit 能力。

### Web Agent Subgraph

计划要求：

- 用 WebAgentSubgraph 取代 BrowserAgentTool 内部大循环。
- Web state 中保存 spec、canonical action trace、last observation、pending action、artifacts、retry state、result、memory context 等。
- Web memory namespace 为 `("sessions", session_id, "web")`。
- canonical action trace 是审计、回放、skill generation 的统一来源。
- `risk_gate` 对 unsafe/unknown risk 调用 `interrupt()`。
- interrupt payload 包含当前页面、待执行动作、目标元素、关键字段、预期影响、风险等级和 resume context。
- confirm 使用 `Command(resume={"decision": "approved"})`。
- unsafe mutation 已确认后只执行一次，不自动重试。
- 支持 crash resume。

### Web Skill Capability Preservation

计划要求：

- 保留 web skill 能力模型。
- 允许整理 schema 到 `workflow.v2`。
- Web subgraph 负责 skill matching、skill execution、skill fallback 和 skill generation trace。
- canonical action trace 取代旧 `task.result.data.steps` 成为正式输入。
- `/save-skill` 优先从 Store 的 web namespace 读取最近成功 task 和 trace。
- 新增 skill 事件。

### Knowledge Subgraph

计划要求：

- KnowledgeSubgraph 取代 `KnowledgeTool` / `KnowledgeWriteTool` 的同步黑盒执行。
- QA 分支拆为 query rewrite、retrieve、emit sources、synthesize、evaluate。
- write 分支拆为 prepare note、write note、reindex。
- Knowledge memory namespace 为 `("sessions", session_id, "knowledge")`。
- retrieval 完成立即 emit `knowledge.sources.ready`。
- answer 完成 emit `knowledge.answer.ready`。
- write 完成 emit `knowledge.write.completed`。
- query rewrite、retrieval、LLM synthesis 可重试。
- vault 未配置、路径不存在、LLM 未启用保持现有用户可读错误。
- knowledge write 不自动重试。

### Memory And ContextCompressor Migration

计划要求：

- 主流程不再调用 `ContextCompressor.compress()` 或 `retrieve()`。
- `_redact_sensitive` / `_truncate` 拆入 `MemorySanitizer`。
- web result extraction 拆入 `WebMemoryStrategy`。
- knowledge QA/source extraction 拆入 `KnowledgeMemoryStrategy`。
- rolling/session summary 使用 LangGraph short-term summary strategy 或领域 summary node。
- legacy metadata sync 拆入 `LegacySessionMemoryMigrator`。
- 首次加载旧 session 时迁移 browser_memory、qa_memory、task_index。
- 旧 session JSON 保留兼容期。

### Event Streaming And APIs

计划要求：

- `AgentController.run(...) -> Task` 保留。
- 新增 `AgentController.stream_run(...) -> Iterator[ProgressEvent]`。
- `AgentController.confirm(...)` 使用 `Command(resume=...)` 恢复 interrupt。
- 新增 state debug API。
- 标准化 ProgressEvent details。
- 关键事件包括 main graph、web action、knowledge、summary、task completed 等。

### Rollout Plan

最初 rollout 共有 9 步：

1. 引入 checkpointer、Store、`langmem`，主图支持 `stream_run()`，保持 `run()` 兼容。
2. CLI/chat 从 progress callback 迁到 stream 消费。
3. 将 web agent 大循环拆成 WebAgentSubgraph，并产出 canonical action trace。
4. 将 web confirmation 和 plan confirmation 改为 LangGraph interrupt/resume。
5. 将 web skill matching、execution、fallback、generation trace 迁入 WebAgentSubgraph。
6. 将 knowledge query/write 拆成 KnowledgeSubgraph，保持结果等价。
7. 引入 web/knowledge memory namespace，并迁移旧 session memory。
8. 移除主流程对 ContextCompressor 的运行时调用。
9. 增强 retry、timeout、state inspection、state history、crash resume 测试。

## 当前整体完成状态

所有 rollout 核心项均已完成。

| 计划项 | 当前状态 |
| --- | --- |
| 引入 checkpointer / Store / langmem | 已完成 |
| 主图 stream_run / run 兼容 | 已完成 |
| CLI/chat 迁移到 stream | 已完成 |
| WebAgentSubgraph | 已完成 |
| canonical action trace | 已完成 |
| plan confirmation interrupt/resume | 已完成 |
| web confirmation interrupt/resume | 已完成 |
| web skill 迁入 Web 子图 | 已完成 |
| KnowledgeSubgraph | 已完成 |
| web/knowledge memory namespace | 已完成 |
| legacy session memory migration | 已完成 |
| 移除 ContextCompressor 运行时依赖 | 已完成 |
| retry / state inspection / crash resume tests | 已完成 |
| checkpoint state 可序列化硬化 | 已完成 |
| langmem 真实配置入口 | 已完成 |
| Knowledge native QA 细粒度节点 retry | 已完成 |

## 依赖与持久化实施

### pyproject.toml

已新增并收紧依赖：

```toml
"langgraph>=1.2.2,<2.0.0",
"langgraph-checkpoint-sqlite>=3.1.0,<4.0.0",
"langmem>=0.0.30,<0.1.0",
```

当前验证环境中实际安装版本：

```text
langgraph==1.2.2
langgraph-checkpoint==4.1.1
langgraph-checkpoint-sqlite==3.1.0
langmem==0.0.30
langchain-core==1.4.0
trustcall==0.0.39
```

### .gitignore

已调整 storage 忽略规则：

- 从宽泛 `storage/` 调整为 `/storage/`
- 保持本地运行产物不误入版本库

### LangGraphRuntime

新增文件：

- `src/aiops_agent/agent/runtime.py`

实现内容：

- `LangGraphRuntimeConfig`
- `LangGraphRuntime`
- 默认 SQLite checkpointer
- 默认 file-backed Store
- InMemory checkpointer/store 测试开关
- SQLite saver 不存在时 fallback 到 memory
- runtime close 支持释放 checkpointer context

默认路径：

- checkpoint：`storage/langgraph/checkpoints.sqlite`
- Store：`storage/langgraph/store/`

### FileBackedStore

新增文件：

- `src/aiops_agent/storage/langgraph_store.py`

实现内容：

- file-backed LangGraph Store
- `put`
- `get`
- `search`
- `list_namespaces`
- namespace path 化
- 本地 JSON 持久化

用途：

- 开发/本地环境长期 memory
- session web/knowledge namespace
- `/save-skill` trace source

## Checkpoint State 可序列化硬化

新增文件：

- `src/aiops_agent/agent/state_codec.py`

这是最后收口阶段新增的关键兼容层。

目的：

- 保证 graph checkpoint state 只保存 dict/list/str/int/float/bool/None。
- 避免未来 LangGraph serializer 收紧后，dataclass/Pydantic/live object 无法恢复。
- 让节点内部仍能以 dataclass 写业务逻辑，降低大范围重构风险。

支持转换对象：

- `Task`
- `AgentSession`
- `ExecutionPlan`
- `ToolCallSpec`
- `ToolExecutionResult`
- `TaskArtifact`
- `BrowserTaskSpec`
- `BrowserAction`
- `BrowserObservation`
- `ActionResult`
- `InteractiveElement`
- LangChain `Document`

适配位置：

- Main Graph node wrapper
- WebAgentSubgraph node wrapper
- KnowledgeSubgraph node wrapper
- Web resume checkpoint 读取
- Knowledge run 初始输入
- Controller confirm/resume 返回值还原

这一步完成后，最初计划中的 state 规则才算真正收口：

- graph state 只保存可序列化数据
- live browser/page/context 不进入 checkpoint

## Main Graph 实施

主要文件：

- `src/aiops_agent/agent/controller.py`
- `src/aiops_agent/agent/progress.py`
- `src/aiops_agent/tasks/manager.py`

### 主图节点

已实现固定节点：

```text
intake -> intent_parse -> task_plan -> policy_check -> route_execution -> summarize -> persist_audit
```

节点职责：

- `intake`：创建/恢复 session，创建 task，写 audit，发 `graph.started` / `session.created` / `task.created`
- `intent_parse`：复用 IntentParser，保留 browser site、credential、allowed domains、trace/video、browser channel 等实体补全
- `task_plan`：复用 PlanningService，加载 session memory，生成 plan/tool calls
- `policy_check`：复用 PolicyEngine，计划确认使用 LangGraph interrupt
- `route_execution`：web 路由 WebAgentSubgraph，knowledge 路由 KnowledgeSubgraph，其余工具保留 ToolExecutor
- `summarize`：复用 ResultSummarizer
- `persist_audit`：保存 task/session/audit，并同步 legacy session + Store memory

### stream_run / run

已实现：

- `AgentController.stream_run(...) -> Iterator[ProgressEvent]`
- `AgentController.run(...) -> Task`

行为：

- `run()` 内部消费 `stream_run()`，保持原接口兼容。
- `stream_run()` 使用 background thread 执行 graph。
- progress event 通过 queue 流式返回。
- CLI/chat 已迁移为 stream 消费。

### confirm / interrupt resume

已实现：

- plan confirmation 使用 main graph `interrupt()`
- web confirmation 优先由 WebAgentSubgraph `risk_gate` interrupt
- `confirm()` 使用 `Command(resume={"decision": decision})`
- 支持 `approved` / rejected 类决策
- 拒绝时 mark blocked
- plan 确认后恢复 policy_check 后续路径
- web 确认后恢复 Web 子图 `risk_gate`

### state debug API

已实现：

- `get_state(task_id)`
- `get_state_history(task_id)`
- `get_web_state(task_id)`
- `get_web_state_history(task_id)`

用途：

- 调试 checkpoint
- 验证 interrupt 是否清除
- 验证 Web 子图状态历史

### TaskManager

改造点：

- `create_task()` 支持传入 `task_id`

用途：

- graph invoke 前生成 task_id
- `thread_id = task.id`
- 使 graph checkpoint thread 和 task id 对齐

## Event Streaming 实施

主要文件：

- `src/aiops_agent/agent/progress.py`
- `src/aiops_agent/agent/controller.py`
- `src/aiops_agent/chat.py`
- `src/aiops_agent/cli.py`

### ProgressEvent details 标准化

已扩展字段：

- `trace_id`
- `task_id`
- `session_id`
- `graph`
- `node`
- `step_index`
- `risk_level`
- `status`
- `current_url`
- `artifact_paths`
- `interrupt_id`

Controller `_emit()` 会补充：

- trace_id
- task_id
- session_id
- graph = main
- node 根据 stage 推导
- status

### 已实现关键事件

Main graph：

- `graph.started`
- `session.created`
- `session.resumed`
- `task.created`
- `intent.parsed`
- `plan.generated`
- `policy.checked`
- `tool.running`
- `summary.ready`
- `task.completed`
- `graph.interrupted`

Interrupt：

- `interrupt.requested`
- `confirmation.confirmed`
- `confirmation.rejected`

Web：

- `web.action.proposed`
- `web.action.executed`
- `web.page.observed`
- `web.skill.matched`
- `web.skill.executing`
- `web.skill.fallback`
- `web.skill.trace.ready`

Knowledge：

- `knowledge.sources.ready`
- `knowledge.answer.ready`
- `knowledge.write.completed`

## CLI / Chat 实施

主要文件：

- `src/aiops_agent/cli.py`
- `src/aiops_agent/chat.py`

### CLI

已完成：

- run 命令使用 `stream_run()`。
- chat 命令使用 stream 消费。
- confirm 命令复用 controller confirm。
- create_controller 接入 LangGraph runtime。
- create_controller 注册 browser agent 后注入 checkpointer/store。
- create_controller 注册 knowledge tool/write tool，并共享 KnowledgeEngine。
- create_controller 构建 WebSkillStore/Matcher/Generator。
- create_controller 增加 `build_session_summary_strategy()`，按配置启用 langmem summary。

### Chat

已完成：

- ChatRunner 从旧 progress callback 迁到 stream 消费。
- 支持持续输出 task progress。
- 保持一般聊天、确认、skill save 等交互能力。

## WebAgentSubgraph 实施

主要文件：

- `src/aiops_agent/browser/subgraph.py`
- `src/aiops_agent/browser/agent.py`
- `src/aiops_agent/browser/planner.py`
- `src/aiops_agent/browser/action_trace.py`

### WebAgentSubgraph 节点

已实现节点：

```text
prepare_spec -> load_web_memory -> restore_browser_context -> plan_action
-> stabilize_action -> risk_gate -> execute_action -> observe_page
-> reflect -> route_next -> skill_fallback -> finalize
```

比最初计划多一个 `skill_fallback` 节点：

- 原计划要求 Web subgraph 负责 skill fallback。
- 实现时将 fallback 拆为显式节点，属于计划内能力展开，不是方向偏离。

### BrowserAgentTool 改造

`BrowserAgentTool.execute()` 已委托给 `WebAgentSubgraph.run()`。

新增：

- `configure_langgraph_runtime(checkpointer, store)`
- `get_state(thread_id)`
- `get_state_history(thread_id)`

内部拆分旧 `_execute_action`：

- `_prepare_runtime_action_for_risk`
- `_record_action_proposed`
- `_awaiting_confirmation_result`
- `_execute_runtime_action`

### Web runtime cache

实现约束：

- live browser tool 保存在 `WebAgentSubgraph._contexts`
- active tool 保存在 `BrowserAgentTool._active_tools`
- checkpoint state 不保存 browser/page/context
- crash resume 时按 checkpoint spec + browser_state_path 重建 browser tool

### Web risk interrupt

`risk_gate` 中：

- 对 `requires_confirmation` 的 unsafe/unknown action 调用 `interrupt(payload)`
- interrupt 前 task 标记为 `awaiting_confirmation`
- payload 包含：
  - `status`
  - `confirmation_type`
  - `resume_node`
  - `task_id`
  - `session_id`
  - `web_run_id`
  - `web_thread_id`
  - `resume_context`
  - `langgraph`
  - `risk_gate`
  - pending action
  - current page/url
  - expected outcome

确认恢复：

- `Command(resume={"decision": "approved"})`
- 拒绝则 blocked
- 已确认 unsafe action 会清除 `requires_confirmation`

### Crash resume

已实现：

- live browser 存在时优先复用
- 进程重启后通过 checkpoint values 恢复 spec/steps/artifacts
- 使用 `browser_state_path` 创建新的 Playwright tool
- 测试覆盖“新 Controller confirm 旧 task”

### Web transient retry

`_execute_runtime_action()` 支持最多 2 次 transient retry。

会 retry：

- safe/read action
- transient browser failure
- locator timeout / navigation 类短暂失败

不会 retry：

- unsafe/unknown risk action
- requires_confirmation action
- login_submit
- system missing 语义失败
- 已确认的 unsafe mutation

### 防止 unsafe mutation 重复执行

`BrowserPlanner` 增加逻辑：

- 检测 remote mutation 是否已经成功执行
- 避免 planner 再次提出同一 unsafe mutation

### Web checkpoint secret 安全

实现：

- `_checkpoint_safe_spec()` 清除 `credential_username`
- `_checkpoint_safe_spec()` 清除 `credential_password`
- planning login action 时通过 `_runtime_spec()` 临时注入 credential
- checkpoint/history 测试确认不包含 secret username/password

## Canonical Action Trace 实施

新增文件：

- `src/aiops_agent/browser/action_trace.py`

实现内容：

- `build_canonical_action_trace`
- `legacy_steps_from_canonical_trace`
- secret redaction
- pending action 支持
- task/session metadata
- schema version

用途：

- 审计
- 回放
- WebSkillGenerator 输入
- `/save-skill` Store trace source
- 旧 steps adapter

BrowserAgentTool 返回中已包含：

- `canonical_action_trace`
- legacy `steps`

WebSkillGenerator 优先使用 canonical trace。

## Web Skill 实施

主要文件：

- `src/aiops_agent/browser/subgraph.py`
- `src/aiops_agent/browser/skills/generator.py`
- `src/aiops_agent/browser/skills/renderer.py`
- `src/aiops_agent/browser/skills/validator.py`
- `src/aiops_agent/browser/skills/matcher.py`
- `tests/test_web_skills.py`

### Skill matching 迁入 Web 子图

实现：

- WebAgentSubgraph `_apply_skill_match()` 调用 matcher
- 命中后将 `auto_plan=False`
- 固定 actions 注入 params
- 记录 skill name、score、parameters、matched keywords

### Skill execution

实现：

- fixed workflow 通过 BrowserAgentTool 既有 fixed action path 执行
- 支持 login action
- 支持 site config workflow
- 支持自然语言 entities 推断参数

### Skill fallback

实现：

- skill 执行失败后，如果允许 fallback，则进入 `skill_fallback`
- fallback 到 LLM planner 一次
- 不对系统缺失信息、登录失败、站点不可用等场景 fallback
- result data 记录 `skill_fallback`

### workflow.v2

已支持：

- schema version `opsagent.web_skill.workflow.v1`
- schema version `opsagent.web_skill.workflow.v2`
- v2 `steps`
- v1 renderer 临时兼容

### /save-skill source

实现：

- 优先从 Store namespace `("sessions", session_id, "web")` 读取 context
- 使用 `last_success_task_id`
- 读取 `trace:{task_id}` 中的 canonical trace
- fallback 到旧 session metadata/task JSON

### 保留能力

已保留：

- 敏感动作过滤
- 登录/密码/登录提交不沉淀
- 参数化字段识别
- answer contract
- terminal reflection 拒绝沉淀
- finish 动态结果不固化
- match score 逻辑
- fallback 一次

## KnowledgeSubgraph 实施

主要文件：

- `src/aiops_agent/agent/knowledge_subgraph.py`
- `src/aiops_agent/knowledge/engine.py`
- `src/aiops_agent/tools/knowledge.py`
- `tests/test_langgraph_runtime.py`

### KnowledgeSubgraph 节点

QA 分支：

```text
validate_config -> load_knowledge_memory -> rewrite_query -> retrieve
-> emit_sources -> synthesize -> evaluate_or_skip -> finalize
```

Write 分支：

```text
validate_config -> load_knowledge_memory -> prepare_note -> write_note
-> reindex_or_skip -> finalize
```

### Legacy fallback

为了保持未配置 vault / LLM disabled / path missing 的现有用户提示：

- 如果 native QA 条件不满足，仍调用旧 `KnowledgeTool.execute()`
- 保持现有 `missing_info`
- 保持现有结果结构
- 保持测试兼容

### Native QA 路径

如果满足：

- `call_spec.tool_name == "knowledge"`
- registry 可拿到 KnowledgeTool
- vault_path 已配置且存在
- LLM config enabled
- KnowledgeTool 有 engine

则走 native QA 分节点执行。

KnowledgeEngine 新增方法：

- `rewrite_query`
- `retrieve_documents`
- `synthesize_answer`
- `evaluate_answer`

### Retry state

每个 native QA 阶段记录 retry state：

- `rewrite_query`
- `retrieve`
- `synthesize`
- `evaluate`

最多重试：

- `MAX_KNOWLEDGE_RETRIES = 2`

写入分支：

- 不自动 retry

### Knowledge events

已实现：

- `knowledge.sources.ready`
- `knowledge.answer.ready`
- `knowledge.write.completed`

Controller 会从 KnowledgeSubgraph result/events 转换为 ProgressEvent。

### Knowledge memory

Knowledge namespace：

```text
("sessions", session_id, "knowledge")
```

保存：

- recent QA summaries
- recent sources
- last answer summary
- rewritten query history 预留
- health/evaluation
- last write

## Memory 迁移实施

主要文件：

- `src/aiops_agent/agent/memory.py`
- `src/aiops_agent/agent/context.py`
- `tests/test_langgraph_memory.py`
- `tests/test_session_memory.py`

### 新增 memory 策略

新增类：

- `MemorySanitizer`
- `WebMemoryStrategy`
- `KnowledgeMemoryStrategy`
- `LegacySessionMemoryMigrator`
- `LegacySessionMemoryWriter`
- `LangMemSummaryStrategy`
- `SessionMemoryManager`

### MemorySanitizer

负责：

- sensitive key redaction
- password/token/credential/secret/api_key/cookie 过滤
- 文本截断
- list 长度限制
- value 统一安全字符串化

### WebMemoryStrategy

负责：

- 从 session/task/result 中提取 web memory
- browser_state_path
- last_url
- last_page_type
- recent_pages
- last_success_task_id
- last_success_site_key
- workflow_hints
- skill_refs
- canonical trace item

### KnowledgeMemoryStrategy

负责：

- recent QA summaries
- recent sources
- last answer summary
- health/evaluation
- last_write

### LegacySessionMemoryMigrator

负责：

- 首次加载旧 session 时迁移 legacy memory 到 Store
- browser_memory -> web namespace
- qa_memory -> knowledge namespace
- task_index -> task_index namespace
- migration marker 防重复迁移

### LegacySessionMemoryWriter

负责：

- 保持旧 session JSON 兼容写入
- short_term
- browser_memory
- qa_memory
- task_index
- metadata legacy fields
- rolling_summary
- summary

### SessionMemoryManager

负责：

- runtime memory sync
- Store retrieve
- web/knowledge namespace 管理
- task_index search
- 从当前 session 刷新 Store context，避免 file-backed Store 读到旧值

### ContextCompressor 状态

当前状态：

- 不再作为 runtime memory framework 使用
- 不再由 Controller 调用 `compress()` / `retrieve()`
- 保留为 legacy shim
- 测试仍用它验证旧格式兼容

新增 docstring 明确：

- legacy session-memory shim
- 只用于迁移测试和 JSON 兼容
- 不应重新作为中心化 memory layer 引入

### langmem summary

`LangMemSummaryStrategy`：

- 使用 `langmem.short_term.summarize_messages`
- 有 model 时调用 langmem
- 无 model 时返回空字符串并回落规则 summary

配置入口：

```json
{
  "langmem_summary": {
    "enabled": true,
    "max_tokens": 1024,
    "max_summary_tokens": 256
  }
}
```

环境变量：

```text
AIOPS_LANGMEM_SUMMARY_ENABLED
```

CLI 注入：

- `build_session_summary_strategy(llm_config, provider)`
- provider `build_summary_model()`
- 默认使用 summary role model

## Security 实施

### Checkpoint security

已覆盖：

- Web spec checkpoint 不保存 `credential_username`
- Web spec checkpoint 不保存 `credential_password`
- pending action / canonical trace secret redaction
- Store memory sanitizer 过滤 password/token/credential/secret/cookie
- Web skill generator 过滤登录/密码/登录提交动作

### 测试覆盖

已新增或更新测试确认：

- Web checkpoint history 不包含登录密码/用户名
- canonical trace redacts secrets
- skill workflow 不沉淀敏感动作
- Store trace redacts password action value

## Testing 实施

### 新增测试文件

- `tests/test_langgraph_runtime.py`
- `tests/test_langgraph_memory.py`
- `tests/test_browser_action_trace.py`

### 重点更新测试文件

- `tests/test_phase2_resume.py`
- `tests/test_browser_workflow.py`
- `tests/test_session_memory.py`
- `tests/test_web_skills.py`
- `tests/test_chat.py`

### 覆盖能力

Runtime：

- FileBackedStore persist/search/list namespaces
- LangGraphRuntime store/checkpointer backend
- main graph stream event order
- main graph state/history
- plan confirmation interrupt/resume

Web：

- WebAgentSubgraph fixed nodes
- Web risk interrupt 在 `risk_gate`
- Web graph interrupt clear after confirm
- Web crash resume from checkpoint
- transient read action retry
- unsafe mutation 不重复执行
- checkpoint 不保存 login secret
- skill match in subgraph
- skill fallback to planner
- canonical trace

Knowledge：

- knowledge sources event before answer
- KnowledgeSubgraph state thread id
- QA retry retryable result
- write 不 retry
- native QA retrieve stage retry

Memory：

- Store web namespace sync
- Store knowledge namespace sync
- migration marker
- task index search
- legacy session JSON compatibility
- langmem summary strategy
- runtime 不调用 legacy ContextCompressor

Chat/CLI：

- stream output
- final report 不重复
- confirmation behavior
- chat context

### 最终测试结果

执行命令：

```bash
.venv/bin/python -m pytest
```

最终结果：

```text
217 items collected
208 passed
9 skipped
1 warning
```

唯一 warning：

```text
trustcall/_base.py:46: LangGraphDeprecatedSinceV10:
Importing Send from langgraph.constants is deprecated.
```

该 warning 来自第三方 `trustcall`，不是当前仓库代码触发。

## 文件级变更总览

### 新增核心文件

`src/aiops_agent/agent/runtime.py`

- LangGraph runtime config
- checkpointer/store 创建
- SQLite saver fallback

`src/aiops_agent/storage/langgraph_store.py`

- file-backed Store
- namespace JSON persistence
- search/list namespace

`src/aiops_agent/agent/state_codec.py`

- checkpoint state serialization boundary

`src/aiops_agent/agent/memory.py`

- memory sanitizer
- web/knowledge memory strategies
- legacy migration/writer
- langmem summary strategy
- session memory manager

`src/aiops_agent/agent/knowledge_subgraph.py`

- KnowledgeSubgraph QA/write 分支
- native QA staged execution
- retry state
- result conversion

`src/aiops_agent/browser/subgraph.py`

- WebAgentSubgraph
- risk interrupt
- skill fallback
- crash resume
- checkpoint safe state

`src/aiops_agent/browser/action_trace.py`

- canonical action trace
- legacy steps adapter
- secret redaction

### 修改核心文件

`src/aiops_agent/agent/controller.py`

- Main Graph
- stream_run
- confirm resume
- memory sync
- knowledge/web subgraph route
- event emission
- state debug APIs

`src/aiops_agent/agent/progress.py`

- ProgressEvent details 扩展

`src/aiops_agent/agent/context.py`

- ContextCompressor 标记为 legacy shim

`src/aiops_agent/browser/agent.py`

- BrowserAgentTool 委托 WebAgentSubgraph
- runtime action execution 拆分
- transient retry
- runtime injection
- get_state/history

`src/aiops_agent/browser/planner.py`

- 避免已成功 remote mutation 重复提出

`src/aiops_agent/browser/skills/generator.py`

- canonical action trace 优先

`src/aiops_agent/browser/skills/renderer.py`

- v2 steps 支持

`src/aiops_agent/browser/skills/validator.py`

- v1/v2 schema 支持

`src/aiops_agent/chat.py`

- chat 迁移到 stream_run

`src/aiops_agent/cli.py`

- CLI 迁移到 stream
- create_controller 注入 runtime/store/checkpointer
- langmem summary strategy builder

`src/aiops_agent/config.py`

- langmem_summary config
- config validation

`src/aiops_agent/knowledge/engine.py`

- query pipeline 拆为公开阶段方法

`src/aiops_agent/llm/base.py`

- `build_summary_model()` 接口

`src/aiops_agent/llm/langchain_provider.py`

- summary model builder

`src/aiops_agent/tasks/manager.py`

- create_task 支持指定 task_id

`pyproject.toml`

- LangGraph/sqlite/langmem 依赖新增并收紧

`.gitignore`

- storage 忽略规则调整

## Rollout Plan 对照

### 1. 引入 checkpointer、Store、langmem，主图 stream_run

状态：完成。

实现：

- `LangGraphRuntime`
- `FileBackedStore`
- `stream_run`
- `run` 兼容
- `langmem` dependency
- `LangMemSummaryStrategy`

### 2. CLI/chat 从 callback 迁到 stream

状态：完成。

实现：

- CLI run 消费 stream
- ChatRunner 消费 stream
- progress callback 仍通过兼容路径保留

### 3. Web agent 大循环拆成 WebAgentSubgraph

状态：完成。

实现：

- `WebAgentSubgraph`
- BrowserAgentTool 委托 subgraph
- fixed graph nodes
- browser context runtime cache

### 4. Web confirmation 和 plan confirmation 改为 interrupt/resume

状态：完成。

实现：

- plan confirmation 在 main graph `policy_check`
- web confirmation 在 Web subgraph `risk_gate`
- `Command(resume=...)`
- confirm 后 checkpoint interrupt 清除

### 5. Web skill 迁入 WebAgentSubgraph

状态：完成。

实现：

- skill matching
- skill execution
- skill fallback
- canonical trace
- v2 schema
- `/save-skill` Store source

### 6. Knowledge query/write 拆成 KnowledgeSubgraph

状态：完成。

实现：

- QA branch
- write branch
- sources/answer/write events
- native QA staged execution
- legacy fallback

### 7. 引入 web/knowledge memory namespace，迁移旧 session memory

状态：完成。

实现：

- `SessionMemoryManager`
- `WebMemoryStrategy`
- `KnowledgeMemoryStrategy`
- `LegacySessionMemoryMigrator`
- Store namespace
- task index namespace

### 8. 移除主流程对 ContextCompressor 的运行时调用

状态：完成。

实现：

- Controller 不再构造/调用 ContextCompressor
- `context_compressor` 参数保留但不使用
- 测试确认不会调用 legacy compressor
- ContextCompressor 标记 legacy shim

### 9. 增强 retry、timeout、state inspection、state history、crash resume 测试

状态：完成。

实现：

- Web transient retry
- Knowledge retry
- state/history APIs
- crash resume tests
- web checkpoint secret tests
- native QA retry tests

## Test Plan 对照

| Test Plan 项 | 当前状态 |
| --- | --- |
| Regression | 已通过全量测试 |
| Persistence | SQLite/file Store/state history 已覆盖 |
| Streaming | event order / chat stream 已覆盖 |
| Interrupt | plan + web interrupt/resume 已覆盖 |
| Web success | artifacts/observation/trace/memory 已覆盖 |
| Web skill | match/fallback/v2/canonical trace/save source 已覆盖 |
| Web resume | live/restart resume 已覆盖 |
| Knowledge QA | sources before answer、retry、state 已覆盖 |
| Knowledge write | write event、不自动 retry 已覆盖 |
| Migration | legacy browser/qa/task_index metadata 迁移已覆盖 |
| Security | checkpoint/Store/trace/skill secret redaction 已覆盖 |

## 方向是否偏离

没有方向性偏离。

有两个实现细节属于计划内展开：

1. Web 子图比最初节点多了 `skill_fallback`。

原因：

- 最初计划明确要求 Web subgraph 负责 skill fallback。
- 将 fallback 拆成显式节点更符合 LangGraph 子图职责。
- 属于计划能力的节点化展开。

2. `ContextCompressor` 没有删除。

原因：

- 初始计划允许作为迁移参考或临时 shim。
- legacy session migration 测试仍需要它验证旧格式。
- 运行时已经不再依赖它。
- 已标记 legacy shim，并加测试防止重新引入运行时调用。

## 当前剩余事项

从最初 LangGraph 深度升级计划看，核心 todo 已完成。

剩余事项属于生产交付和维护，不是计划缺口：

1. 如果项目需要完全可复现安装，需要补 lockfile 或依赖锁定文件。
2. 等兼容期结束后，可删除或进一步隔离 `ContextCompressor` legacy 测试。
3. 关注第三方 `trustcall` 的 LangGraph v2 deprecation warning。
4. 建议对这次大变更做人工 code review。
5. review 后再 stage/commit。

## 当前 Git 状态说明

当前变更尚未提交。

新增文件包括：

- `langgraph-compatible-20260602.md`
- `src/aiops_agent/agent/runtime.py`
- `src/aiops_agent/agent/memory.py`
- `src/aiops_agent/agent/state_codec.py`
- `src/aiops_agent/agent/knowledge_subgraph.py`
- `src/aiops_agent/browser/subgraph.py`
- `src/aiops_agent/browser/action_trace.py`
- `src/aiops_agent/storage/langgraph_store.py`
- `tests/test_browser_action_trace.py`
- `tests/test_langgraph_memory.py`
- `tests/test_langgraph_runtime.py`

主要修改文件包括：

- `.gitignore`
- `pyproject.toml`
- `src/aiops_agent/agent/context.py`
- `src/aiops_agent/agent/controller.py`
- `src/aiops_agent/agent/progress.py`
- `src/aiops_agent/browser/agent.py`
- `src/aiops_agent/browser/planner.py`
- `src/aiops_agent/browser/skills/generator.py`
- `src/aiops_agent/browser/skills/renderer.py`
- `src/aiops_agent/browser/skills/validator.py`
- `src/aiops_agent/chat.py`
- `src/aiops_agent/cli.py`
- `src/aiops_agent/config.py`
- `src/aiops_agent/knowledge/engine.py`
- `src/aiops_agent/llm/base.py`
- `src/aiops_agent/llm/langchain_provider.py`
- `src/aiops_agent/tasks/manager.py`
- 多个测试文件

## 最终结论

截至 2026-06-02，opsAgent LangGraph 深度升级已经完成从最初计划到当前实现的主体落地。

当前已经具备：

- LangGraph 原生 Main Graph
- SQLite checkpointer
- file-backed LangGraph Store
- stream_run event API
- LangGraph interrupt/resume
- plan confirmation resume
- WebAgentSubgraph
- Web risk_gate interrupt
- Web crash resume
- canonical action trace
- Web skill matching/execution/fallback/generation trace
- KnowledgeSubgraph
- Knowledge QA native staged execution
- Knowledge write branch
- web/knowledge memory namespace
- legacy session memory migration
- langmem summary 配置入口
- checkpoint state 可序列化边界
- security redaction
- state/history debug API
- 全量测试通过

这版实现已经可以进入 review / commit 阶段。
