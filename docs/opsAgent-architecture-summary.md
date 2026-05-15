# opsAgent 代码与功能架构梳理

配套架构图：[`opsAgent-technical-architecture.svg`](./opsAgent-technical-architecture.svg)

## 一句话定位

`opsAgent` 是一个受控的 AIOps 编排内核：用户通过 CLI 或 Chat 输入自然语言运维任务，系统先识别意图、生成结构化执行计划，再经过策略检查和工具协议执行，最后把结果、会话上下文、审计事件和浏览器 artifacts 持久化到本地。

它不是让 LLM 直接操作系统，而是让 LLM 参与理解、规划、问答合成和浏览器下一步决策；真正执行始终受 `Task` 状态机、`PolicyEngine`、`ToolCallSpec`、`ToolRegistry`、浏览器风险评估和人工确认机制约束。

## 分层设计

### 1. 接入层

主要代码：`src/aiops_agent/cli.py`、`src/aiops_agent/chat.py`

已实现入口：

- `aiops-agent run`：执行单次自然语言任务。
- `aiops-agent chat`：进入多轮交互，支持 `/new`、`/session`、`/note`、`/save-note`、`/save-skill`。
- `aiops-agent confirm`：恢复等待人工确认的浏览器任务。
- `aiops-agent knowledge index/query/write`：直接管理 Obsidian vault 索引、查询和写入。
- `aiops-agent session list/close`：管理本地会话。

### 2. 配置装配层

主要代码：`src/aiops_agent/config.py`、`src/aiops_agent/browser/site_config.py`、`src/aiops_agent/browser/credentials.py`

`create_controller()` 负责装配运行依赖：

- RPA / ShadowBot 配置与启动校验。
- LLM provider 配置，支持 Anthropic / OpenAI 兼容接口。
- Obsidian vault、索引模式、embedding provider。
- 浏览器站点配置：`site_key`、`base_url`、`login_url`、`login_fields`、工作流字段。
- 浏览器凭据引用：通过 `credential_ref` 或站点默认引用获取。
- 注册工具：`inspection`、`knowledge`、`knowledge_writer`、`chat`、`browser_agent`。

### 3. 编排状态机层

主要代码：`src/aiops_agent/agent/controller.py`

核心对象是 `AgentController`，内部用 LangGraph 构建显式状态机：

1. `intent_parse`：识别任务意图并补充运行参数。
2. `task_plan`：生成 `ExecutionPlan` 和 `ToolCallSpec`。
3. `policy_check`：判断是否放行、阻断或等待确认。
4. `tool_execute`：通过 `ToolExecutor` 执行第一个工具调用。
5. `summarize`：生成面向用户的报告。
6. `persist_audit`：压缩上下文、保存 task/session、写审计事件。

人工确认恢复由 `confirm()` 单独处理：它加载 `awaiting_confirmation` 任务，把 `pending_action_raw`、`replay_actions`、`session_state_path`、`completed_action_keys` 注入原来的工具调用，然后恢复执行。

### 4. 认知与治理层

主要代码：`agent/parser.py`、`planning.py`、`policy.py`、`agent/summarizer.py`、`agent/context.py`、`llm/*`

职责划分：

- `IntentParser`：优先使用 LLM 分类，失败后走规则 fallback；支持 `inspection`、`permission_change`、`ops_qa`、`knowledge_write`、`web_action`、`general_chat`。
- `PlanningService`：把意图转换为 `ExecutionPlan`，并选定工具和参数；`web_action` 会先尝试匹配已有 Web Skill。
- `PolicyEngine`：处理显式确认和高风险权限变更；浏览器的逐步风险控制在 `BrowserAgentTool` 内完成。
- `ResultSummarizer`：按任务类型输出巡检报告、知识库答案、写入结果、网页答案或失败建议。
- `ContextCompressor`：更新会话滚动摘要、最近页面状态、浏览器 state path、最近成功 web_action、最近 QA 轮次。
- `LangChainLLMProvider`：统一封装意图分类、普通对话、浏览器 ReAct planning、知识库合成等模型调用。

### 5. 工具协议层

主要代码：`tasks/models.py`、`tools/base.py`、`tools/registry.py`、`tools/executor.py`

关键契约：

- `Task`：任务状态、意图、实体、计划、风险、工具调用、结果、报告和 artifacts。
- `ExecutionPlan`：目标、步骤、选用工具、成功标准、风险和确认要求。
- `ToolCallSpec`：工具名、动作、参数、风险、幂等键、超时。
- `ToolExecutionResult`：`success`、`data`、`error`、`retryable`、`artifacts`。
- `ToolRegistry`：工具注册与查找。
- `ToolExecutor`：按 `ToolCallSpec` 统一调用工具。

### 6. 能力实现层

主要代码：`tools/inspection.py`、`tools/knowledge.py`、`knowledge/*`、`browser/*`、`tools/chat.py`

已实现能力：

- RPA 巡检：支持 API 调用 RPA 平台，也支持 Windows 下 ShadowBot 本地启动；统一输出巡检结果、异常列表和操作日志。
- 运维知识问答：对 Obsidian vault 做 Markdown/frontmatter/wikilink 解析，支持 BM25、Chroma 向量、Hybrid RRF 检索，使用 LLM 合成答案并可选评估忠实度与相关性。
- 知识库写入：把显式指令和最近 QA 上下文整理为 Markdown 笔记，写入 vault，更新 MOC，并按配置刷新索引。
- 受控浏览器自动化：基于 Playwright 观察页面、规划动作、执行交互、保存截图和页面摘要；危险远端写入动作先进入人工确认。
- 浏览器 Reflection：每步执行后记录 `action.executed` 和 `page.observed`，再通过 `_reflect_after_action()` 生成 `action.reflected`，输出 `failure_category`、`terminal_reason`、`next_decision`，用于判断继续、停止、业务缺失或可回退。
- Web Skill：从成功的 `web_action` 沉淀可复用 `SKILL.md + workflow.json`，后续相似任务可直接走固定动作流，失败时可回退 LLM planner 一次。
- Chat：普通对话使用当前时间上下文，不误触发运维工具。

### 7. 支撑与外部层

主要代码：`storage/*`、`audit/*`、`sessions/models.py`

本地持久化：

- `storage/tasks/*.json`：任务状态、计划、结果、工具调用、artifacts。
- `storage/sessions/*.json`：会话状态、滚动摘要、最近页面、QA 上下文、最近成功 web_action。
- `storage/audit/events.jsonl`：审计事件，包含会话、计划、策略、工具、浏览器动作和页面观察；敏感字段会脱敏。
- `storage/artifacts/<session>/<task>/`：截图、页面摘要、执行报告、浏览器 state、trace、video。
- `storage/web_skills/<skill>/`：沉淀出来的 Web Skill。

外部系统：

- RPA 平台或 ShadowBot。
- Obsidian vault 及其 `.chroma` 向量索引。
- 企业目标 Web 系统。
- 外部 LLM provider。

## 主链路

用户输入后，CLI/Chat 创建 Controller；Controller 创建或恢复 Session，并创建 Task；状态机依次完成意图识别、计划生成、策略检查、工具执行、结果摘要和持久化。所有关键节点都会写审计事件。若浏览器任务遇到危险动作，工具返回 `awaiting_confirmation`，用户确认后通过 `confirm()` 恢复浏览器上下文并继续执行。

## 关键设计点

- LLM 不直接越权执行：所有执行都落到工具协议和策略约束。
- Web 自动化是逐步计划和逐步观察，不是一次性脚本。
- 远端写入类网页动作默认需要人工确认。
- 知识库同时支持问答和写入，且写入后可自动刷新索引。
- 会话层保留 QA 轮次、浏览器状态和最近成功任务，为追问、恢复和 skill 沉淀服务。
- 审计层贯穿会话、任务、策略、工具、浏览器动作和页面观察，适合企业运维追踪。
