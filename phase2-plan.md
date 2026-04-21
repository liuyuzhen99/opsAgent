## Phase 2：Playwright Agent 化、日志体系与最小 Agent 基础能力

### Summary

Phase 2 不再以影刀集成为主，而是把当前项目从“单轮意图识别 + 工具调用”的 CLI 原型，升级为一个最小可用的 Web Agent MVP。核心目标有三条：

- 接入 Playwright，支持用户用自然语言描述“想在某个网站上做什么”，Agent 能在受控边界内自主规划并执行网页操作
- 完善日志与审计体系，让每一步网页决策、页面观察、工具调用、确认节点都可追踪
- 补齐最小 Agent 基础能力：会话管理、上下文压缩，以及可恢复执行所需的任务级与会话级记忆

本阶段按既定偏好收敛为：

- Playwright 采用“高度自主浏览”方向，但执行层必须受控
- 记忆先覆盖任务级 + 会话级，不做长期用户画像
- 登录默认支持 Agent 使用账号密码登录
- 任何可能产生远端副作用的动作，在执行前必须人工确认

Phase 2 的交付原则不是“一次把所有基础设施补齐”，而是先交付一个可运行、可停止、可追踪、可审计的 Web Agent MVP，再在此之上补完恢复能力与记忆能力。

### Delivery Tiers

- `P0 / MVP 必做`
  - `web_action` 任务识别与结构化任务规格提取
  - 最小 Browser Agent 执行循环
  - 受限动作集合 + 结构化 observation
  - 副作用动作前人工确认
  - 失败/阻塞时自动保存截图与关键页面摘要
  - 基础运行日志与关键审计事件

- `P1 / Phase 2 内优先补齐`
  - session 创建、恢复、关闭、列出
  - 同 session 下浏览器上下文复用与登录态复用
  - 上下文压缩与滚动摘要
  - 中断恢复时的任务摘要加载

- `P2 / Phase 2 内可选增强`
  - 结构化任务级/会话级记忆
  - 审计日志与运行日志彻底分层
  - Playwright trace/video 开关
  - 更完整的 artifact 归档与执行报告

- `明确延期`
  - 长期跨会话用户画像
  - 多浏览器、多标签复杂编排
  - 绕过验证码、MFA、扫码登录
  - 通用脚本库编排平台

### Key Changes

- 意图层从“任务分类器”升级为“任务分类 + Web 自动化规格提取”
  - 在现有 `inspection / permission_change / ops_qa` 基础上新增明确的 `web_action` 或等价任务类型，用于承载自然语言网页操作任务。
  - LLM 输出不只包含 `intent`，还要提取目标站点、目标动作、预期结果、约束条件、是否涉及登录、是否涉及副作用动作。
  - 对网页任务的目标表达统一收敛为结构化执行规格，例如：站点入口、用户目标、成功判定、禁止操作、候选凭据键名、允许访问的域名范围。

- 新增 Browser Agent 执行循环
  - 不采用“固定脚本库 + 参数填充”作为主路径，而是设计一个最小 ReAct/Planner-Executor 循环。
  - 循环结构建议为：任务理解 -> 浏览器初始化 -> 页面观察 -> 生成下一步动作 -> 执行动作 -> 回读页面状态 -> 判断是否完成/是否需要确认/是否需要重试。
  - 动作集合必须是受限的高层 Playwright 工具，而不是让模型直接拼任意代码。
  - 第一版动作建议控制在：`open_url`、`observe_page`、`click`、`type`、`select`、`press`、`wait_for`、`extract_text`、`save_artifact`、`finish`、`request_confirmation`。
  - 每一步都要返回结构化 observation，供下一轮决策使用，避免纯文本自由发挥。

- 执行状态机与停止条件
  - Browser Agent 必须显式维护执行状态，而不是隐式地靠 prompt 驱动循环。
  - 最低状态集合建议为：`planning`、`acting`、`observing`、`awaiting_confirmation`、`blocked`、`completed`、`failed`。
  - 需要定义允许的状态迁移，避免出现“确认中继续执行”或“失败后仍继续点击”。
  - 必须设置运行预算：`max_steps`、`max_consecutive_failures`、同页重复动作阈值、单动作超时。
  - 以下情况默认进入 `blocked` 或 `failed`：跳转到非允许域名、进入 MFA/验证码、连续定位失败、页面长时间不稳定、无法判断是否存在副作用。

- Playwright 集成方式
  - 新增浏览器工具层，封装 Playwright 的启动、页面访问、元素定位、交互、截图、DOM 摘要提取。
  - 页面观察不直接把整页 HTML 喂给模型，而是生成压缩后的页面状态摘要。
  - 页面状态摘要至少包含：页面标题、URL、页面类型、主要交互元素列表、可见表单字段、按钮候选、错误提示、最近一次动作结果。
  - 元素定位采用“优先语义定位，回退到稳健选择器”的策略，避免完全依赖脆弱 CSS selector。
  - 对登录页、列表页、表单页、确认弹窗页设计统一 observation schema，减少模型理解负担。

- 自然语言网页任务的 Agent 化方案
  - 用户输入示例不是“执行脚本 X”，而是“登录某站点，找到某用户，给他开某权限，但提交前先给我确认”。
  - LLM 首轮只做两件事：生成任务规格和初始计划，不直接生成 Playwright 代码。
  - 执行过程中模型依据 observation 决定下一步高层动作，实现“边观察边决策”的 Agent 感。
  - 为避免失控，模型不能直接调用任意浏览器原语，只能从受限动作集合中选择，并附带目标元素描述、预期结果和风险等级。
  - 当遇到多步表单、弹窗、跳转、局部加载失败时，Agent 允许有限次数的重新观察与重规划，但必须受运行预算约束。

- 副作用确认机制
  - 不再仅以“最终提交按钮”作为唯一风险边界，而是以“是否可能改动远端状态”作为确认边界。
  - 动作需要做风险分级：
    - `safe_read`：打开、观察、提取、等待，不会改动远端状态。
    - `safe_local_edit`：仅在本地页面形成草稿，且可确认不会触发远端写入。
    - `unsafe_mutation`：保存、提交、切换开关、上传、下载触发、删除、批量修改、创建账号、分配权限等。
    - `unknown_risk`：无法判断是否会产生副作用时，默认阻塞并要求人工确认。
  - 任何 `unsafe_mutation` 或 `unknown_risk` 动作都必须进入 `awaiting_confirmation` 或等价状态。
  - 确认前系统必须输出清晰的人类可读预执行摘要：当前页面、准备执行的动作、关键字段值、预期影响、风险等级。
  - 确认机制先做 CLI 交互式版本即可，不要求本阶段实现复杂审批流。

- 会话管理
  - 新增 session 概念，不再只保存孤立 task。
  - 一个 session 下可包含多轮用户输入、浏览器状态、任务摘要、最近页面观察、确认节点。
  - 浏览器上下文与 session 绑定，允许同一会话内复用登录态、最近访问历史和页面上下文。
  - Session 最低应支持：创建、恢复、关闭、列出当前活跃状态。
  - CLI 建议新增可选参数，例如 `--session-id`，未传时自动创建。

- 上下文压缩
  - 为避免多轮网页操作导致上下文爆炸，引入分层压缩。
  - 保留最近 N 步原始 observation + 一份滚动摘要，旧步骤压缩为会话摘要。
  - 压缩内容应分成三类：用户目标、已经完成的关键动作、当前页面与待办事项。
  - 页面 observation 也要做压缩，只保留与当前目标相关的可交互元素、错误信号与阻塞原因。
  - 当进入确认节点、阻塞节点或任务结束时，再生成一次高质量任务摘要，供后续恢复。

- 记忆系统
  - Phase 2 先做任务级 + 会话级记忆，不做长期用户画像。
  - 任务级记忆保存本轮目标、关键中间结果、失败原因、待确认动作、阻塞原因。
  - 会话级记忆保存当前站点、登录状态、已访问页面、常用实体映射、最近成功操作路径。
  - 记忆应以结构化存储为主，摘要文本为辅，便于恢复与调试。
  - 第一版不要自动跨站点泛化“习惯记忆”，避免错误迁移。

- 凭据与登录能力
  - Phase 2 支持 Agent 使用账号密码执行登录。
  - 凭据不直接写入 task/session 持久化内容，持久化中只保存引用标识和脱敏信息。
  - 建议增加最小凭据提供方式，例如环境变量映射或本地凭据配置文件引用。
  - 登录流程要作为专门的子能力处理：识别登录页、输入用户名、输入密码、提交、检测登录成功/失败/二次验证。
  - 遇到验证码、MFA、短信校验时，本阶段统一进入 `blocked` 状态并提示人工接手，不强行自动化。

- 日志与审计体系升级
  - 当前日志只有通用 trace；Phase 2 需要细化为 Agent 事件流。
  - 新增事件类型建议包括：`session.created`、`session.resumed`、`plan.generated`、`browser.started`、`page.observed`、`action.proposed`、`action.executed`、`action.blocked_for_confirmation`、`action.blocked_for_unknown_risk`、`memory.compressed`、`task.completed`、`task.failed`。
  - 每条日志至少带：trace_id、session_id、task_id、step_index、current_url、action_type、risk_level、result。
  - 审计日志与运行日志分层：
    - 运行日志用于调试执行细节。
    - 审计日志用于回溯用户请求、关键动作、确认记录和最终结果。
  - 对敏感信息做脱敏：密码、cookie、token、个人隐私字段不能原样落盘。

- 存储模型扩展
  - 在现有 `Task` 之外新增 `Session` 模型，以及网页执行步骤记录模型。
  - `Task` 需要增加与 session 的关联、当前阶段、确认状态、浏览器结果摘要、阻塞原因。
  - 存储目录建议扩展出：
    - `storage/sessions/`
    - `storage/tasks/`
    - `storage/audit/`
    - `storage/artifacts/`
  - 页面截图、HTML 摘要、最终执行报告可作为 artifact 存档，以支持排查。

- 浏览器产物与调试能力
  - 执行失败、进入确认节点或进入阻塞节点时，自动保存截图和关键页面摘要。
  - 成功任务也至少保存最终页面截图和执行摘要，方便验证。
  - 如果条件允许，可预留 Playwright trace/video 的开关配置，但不是 Phase 2 必做项。

- 非目标与边界
  - Phase 2 不把影刀结果回收作为主线。
  - Phase 2 不做完整长期记忆或向量知识库。
  - Phase 2 不支持绕过验证码或 MFA。
  - Phase 2 不追求多浏览器、多标签复杂编排，先以单浏览器上下文、单 session 主线为主。

### Public APIs / Interfaces

- CLI 扩展
  - 保留现有 `aiops-agent run "<task_input>"`。
  - 新增可选参数建议：
    - `--session-id`
    - `--browser-config`
    - `--credential-config`
    - `--confirm` 或交互式确认模式开关
    - `--max-steps`
    - `--allowed-domains`

- 新任务类型
  - `web_action`：用于通用网页自动化任务。
  - 现有 `permission_change` 后续可以作为 `web_action` 的高风险特化场景，而不是完全独立另一套执行栈。

- 新核心接口建议
  - `SessionManager.create_or_resume(session_id) -> Session`
  - `BrowserAgent.run(task, session) -> Task`
  - `PlaywrightTool.execute(action: BrowserAction) -> BrowserObservation`
  - `ContextCompressor.compress(session, steps) -> SessionSummary`
  - `MemoryStore.load(session_id) / save(session_state)`
  - `RiskEvaluator.classify(action, observation) -> RiskLevel`

- 结构化动作接口
  - `BrowserAction { type, target_hint, target_id, value, expected_outcome, risk_level, requires_confirmation, timeout_ms }`
  - `BrowserObservation { url, title, page_type, interactive_elements, forms, visible_messages, last_action_result, blocking_reason, screenshot_path, done_signals }`
  - `InteractiveElement { element_id, role, name, text, locator_strategy, is_enabled, is_visible }`

- 状态模型
  - `TaskState { planning | acting | observing | awaiting_confirmation | blocked | completed | failed }`
  - `ActionResult { success | retryable_failure | terminal_failure | blocked_for_confirmation | blocked_for_unknown_risk }`

### Test Plan

- 测试基座
  - 至少准备 1 个稳定测试站点或本地 mock web app，覆盖登录页、列表页、表单页、确认弹窗页、错误提示页。
  - 所有核心自动化用例优先跑在固定测试基座上，避免把“临场演示成功”当成验收标准。

- Playwright Agent 基本链路
  - 场景：用户给出自然语言只读网页任务，例如登录测试站点后搜索用户并读取权限信息。
  - 断言：系统能识别为 `web_action` 并生成结构化计划；Agent 能在受限动作集合内完成打开页面、观察、点击、输入、等待、提取结果的多步循环；日志包含 `plan.generated`、`page.observed`、`task.completed`。
  - 通过标准：在固定测试站点上连续运行 5 次，成功 5 次。

- 页面变化与重规划
  - 场景：页面元素在交互后发生变化，例如搜索结果异步刷新或弹出二级表单。
  - 断言：Agent 能根据新的 observation 调整下一步动作，而不是重复旧动作；若超过重试预算则进入 `failed` 或 `blocked`。
  - 通过标准：无无限循环，所有失败都有明确状态码、错误原因和截图。

- 登录与凭据
  - 场景：账号密码登录成功、用户名错误、密码错误、登录后跳转失败、遇到 MFA。
  - 断言：登录成功路径可继续执行；错误路径有明确错误输出；遇到 MFA/验证码时进入 `blocked` 而不是无限重试。
  - 通过标准：所有登录失败场景都能在 3 次动作失败内收敛到明确状态。

- 风险控制
  - 场景：只读任务、表单草稿任务、真正提交任务、无法判断是否有副作用的任务。
  - 断言：`safe_read` 可全自动完成；`unsafe_mutation` 和 `unknown_risk` 必须在执行前暂停并生成待确认摘要；未确认时不得执行真正提交动作；确认后能继续执行并记录确认审计事件。
  - 通过标准：0 次未确认写入；所有阻塞动作都有审计记录。

- 会话与恢复
  - 场景：同一 session 内执行第二轮任务、中断后恢复 session、恢复后继续执行。
  - 断言：可复用登录态和最近页面上下文；恢复时能读取摘要并继续执行，而不是从零开始；上下文压缩后仍保留足够信息完成后续步骤。
  - 通过标准：恢复后的任务不需要重新登录即可继续的场景成功率 >= 80%。

- 日志与审计
  - 场景：成功、失败、阻塞、确认四类任务都各跑至少 1 次。
  - 断言：每一步浏览器动作都有可追踪日志；失败任务能留下截图、页面摘要和错误原因；审计日志中不泄露密码、token、cookie。
  - 通过标准：抽样检查日志，敏感字段全部脱敏。

- 回归覆盖
  - 场景：现有 `inspection` 流程、非网页任务、LLM 分类失败场景。
  - 断言：现有 `inspection` 流程不被破坏；分类失败时仍有合理降级路径；非网页任务仍可按当前 Phase 1 能力执行或拒绝。
  - 通过标准：现有核心回归用例全部通过。

- 阶段验收标准
  - P0 验收基线：固定测试基座上的只读任务成功率 >= 80%，0 次未确认写操作，所有失败/阻塞均有 artifact 和状态记录。
  - P1 验收基线：session 恢复、登录态复用、上下文压缩在固定场景上稳定可复现。
  - P2 验收基线：记忆与审计增强不破坏 P0/P1 基础链路。

### Assumptions

- Phase 2 默认先支持单浏览器上下文、单 session 主线，不做复杂并发网页任务。
- Playwright 作为新增依赖引入，浏览器运行环境与安装流程需要在实施前补充到工程文档。
- Browser Agent 采用“高度自主浏览”的用户体验目标，但底层必须是受限动作集合，不允许模型直接生成任意自动化代码执行。
- 记忆先做到任务级 + 会话级，不引入长期跨会话用户画像。
- 登录默认支持账号密码，但 MFA、验证码和扫码登录统一视为人工接管场景。
- 任何可能产生远端副作用的动作都必须人工确认，这是本阶段的默认安全基线。
