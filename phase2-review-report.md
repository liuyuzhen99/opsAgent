# Phase 2 Review Report

## 1. 背景与目标

Phase 2 的目标是把 Phase 1 中偏静态的任务执行能力扩展成可控的浏览器 Agent。这里的关键不是“让浏览器能点东西”这么简单，而是要在安全边界内完成一条真实可验收的闭环：

1. 用户提出网页任务。
2. 系统识别为 `web_action`。
3. Planner 生成浏览器任务规格。
4. Browser Agent 打开网页并观察页面。
5. Planner 根据 observation 动态决定下一步。
6. 对只读动作直接执行。
7. 对远程副作用动作阻断并等待确认。
8. 对需要登录的页面，使用凭据引用完成账号密码登录。
9. 任务完成后输出 artifact、报告、审计和 session 状态。

这次实现的重点是完成 Phase 2 的 P0/P1 主体，并补齐此前缺口最大的登录与凭据最小闭环。实现过程中刻意没有把浏览器 Agent 做成不受约束的 Playwright 脚本执行器，因为那会把安全、审计、恢复和测试成本同时放大。

## 2. 总体实现路线

Phase 2 采用了“结构化任务 + 动态 planner + 受控工具层 + 风险门禁”的路线。

核心分层如下：

- 解析层：把自然语言任务识别为 `web_action`，提取 URL、登录需求、远程变更风险、凭据引用等实体。
- 规划层：生成 `browser_agent` tool call，并把浏览器任务描述成 `BrowserTaskSpec`。
- Browser Planner：根据页面 observation 和历史 steps 选择下一步动作。
- Playwright Tool：只暴露有限的高层动作，不暴露任意脚本执行。
- Risk Evaluator：对动作做风险分类，决定是否可自动执行。
- Browser Agent：串起 planner、tool、risk、artifact、audit、session 和确认恢复。

这样拆分的原因是每层承担一个明确责任。自然语言解析不直接碰浏览器，浏览器工具不判断业务意图，风险评估不依赖 UI 细节，session 存储不持有明文凭据。这让系统更容易测试，也降低后续扩展时互相污染的概率。

## 3. Web Action 识别与结构化计划

### 做了什么

规划层新增了 `web_action` 支持。用户输入被识别为网页任务后，会生成一个 `browser_agent` tool call，并携带：

- 目标 URL。
- 是否需要登录。
- 是否可能产生远程副作用。
- 允许访问的 domain。
- `credential_ref`。
- trace/video 开关。
- 是否自动规划。

### 怎么做

`PlanningService` 负责把 parser 提取出的实体转换为 `BrowserTaskSpec` 所需字段。CLI 把 `--allowed-domains`、`--credential-config`、`--credential-ref`、`--browser-trace`、`--browser-video` 等参数传入 controller，再由 controller 注入规划上下文。

### 为什么这样做

Phase 2 的浏览器任务需要比普通命令更强的边界控制。如果只把原始自然语言直接交给浏览器执行器，后续很难判断某一步是否越权、是否需要确认、是否可以恢复。因此先把任务结构化，再进入 Browser Agent。

### 优势与取舍

优势是任务边界清晰，测试可以直接断言 tool call 和 spec 字段。代价是第一版解析能力比较保守，复杂网页目标可能需要用户给出更明确的 URL、domain 或凭据引用。这个取舍适合 Phase 2，因为安全和可验收比泛化理解更重要。

## 4. Browser Planner 动态规划

### 做了什么

新增 `BrowserPlanner`，让浏览器执行不再依赖固定动作数组。Planner 会读取当前 `BrowserObservation` 和已执行 steps，再决定下一步动作。

典型只读流程是：

1. `open_url`
2. `observe_page`
3. `extract_text`
4. `save_artifact`
5. `finish`

典型登录流程是：

1. `open_url`
2. `observe_page`
3. `type_username`
4. `type_password`
5. `login_submit`
6. `extract_text`
7. `save_artifact`
8. `finish`

如果页面是 MFA 或验证码页，Planner 会直接输出 `blocked`。

### 怎么做

Planner 只处理高层动作，不直接操作 DOM。它通过 observation 中的页面类型、输入框、按钮、错误提示和当前 URL 判断下一步。执行结果再次变成新的 observation，形成一个有限状态循环。

### 为什么这样做

固定动作脚本只能覆盖演示页面，一旦页面初始状态不同就会失败。动态 planner 可以处理“已经登录”“还在登录页”“登录失败”“遇到 MFA”这些分支，同时仍然保持规则可读、可测试。

### 优势与取舍

优势是比硬编码脚本更稳，比 LLM 自由规划更可控。代价是它还不是通用网页智能体，对于复杂 UI、非标准登录表单、多步骤向导等场景覆盖有限。Phase 2 选择规则优先，是为了先把安全门禁、凭据和 session 闭环做扎实。

## 5. Playwright 工具层与 Observation Schema

### 做了什么

新增 `PlaywrightBrowserTool`，封装真实浏览器能力，并输出结构化 observation。工具层支持：

- 打开 URL。
- 观察页面。
- 点击、输入、选择、按键。
- 登录提交。
- 等待页面状态。
- 抽取文本。
- 保存 artifact。
- 保存截图和 page summary。
- 保存和加载 `storage_state`。
- 可选开启 trace/video。

Observation 增强了以下识别能力：

- password 输入框。
- username/email/account 输入框。
- 登录按钮。
- 错误提示。
- MFA/验证码页面。
- 可交互元素摘要。

### 怎么做

Playwright 层把 DOM 和可访问性信息压缩成稳定的数据模型，而不是把完整 HTML 暴露给 planner。它还对输入框值做屏蔽，避免页面里已有账号、密码、cookie、token 等敏感信息流入审计和 artifact。

### 为什么这样做

完整 DOM 噪声大、变化频繁，也容易泄漏敏感信息。结构化 observation 更适合规划、测试和审计。工具层只提供有限动作，可以把风险评估建立在动作类型之上。

### 优势与取舍

优势是可控、可测试、可脱敏。代价是 observation 可能丢掉复杂页面里的一些细节。后续如果要增强，可以增加更多字段，而不是让 planner 直接依赖完整 HTML。

## 6. 风险控制与人工确认

### 做了什么

新增浏览器动作风险评估。系统会把动作分成：

- `safe_read`: 只读和保存本地 artifact。
- `safe_local_edit`: 本地输入、选择、按键等尚未提交远程变更的动作。
- `unsafe_mutation`: 远程提交、发布、购买、删除、发送等。
- `unknown_risk`: 无法判断的动作。

当动作属于远程副作用或未知风险时，Browser Agent 不会直接执行，而是把 pending action 写入任务状态并等待人工确认。CLI 提供 `confirm <task_id>` 恢复执行。

### 怎么做

Browser Agent 在每一步执行前调用 risk evaluator。若需要确认，它保存当前 task、pending action、session state 和 resume URL。用户确认后，controller 重新加载任务，取出 pending action，并用同一个 session 状态继续执行。

### 为什么这样做

浏览器 Agent 的最大风险不是读页面，而是误触发真实业务操作。确认门禁把不可逆动作从自动执行路径里拿出来，同时保留自动化在只读和本地编辑上的效率。

### 优势与取舍

优势是安全边界明确，审计链路完整，适合接入真实网站。代价是某些需要频繁提交的小操作会变慢。这个代价是有意接受的，因为 Phase 2 的定位是可信自动化，而不是完全无人值守的远程操作。

## 7. 登录与凭据最小闭环

### 做了什么

本阶段补齐了账号密码登录：

- CLI 支持 `--credential-config` 指向本地 JSON。
- CLI 支持 `--credential-ref` 指定凭据引用。
- 新增 `CredentialStore` 和 `BrowserCredential`。
- Browser Agent 运行时加载凭据，但不写入 task/session。
- Planner 在登录页生成用户名输入、密码输入和登录提交动作。
- MFA/验证码页直接阻断。
- 登录失败保存截图和 page summary。
- 审计日志继续对 password/token/cookie/secret 等字段脱敏。

### 怎么做

凭据配置只作为运行时输入。规划和任务持久化只知道 `credential_ref`，Browser Agent 在实际执行登录动作前才解析引用并拿到 username/password。执行输入动作时，审计事件记录动作类型和目标，不记录明文值。页面 observation 中的输入框值也会被屏蔽。

### 为什么这样做

登录是 Phase 2 从演示走向可用的关键缺口，但凭据是高敏感数据。把凭据限制在运行时内存中，并用引用贯穿任务、审计和 session，是当前复杂度下比较稳妥的最小方案。

### 优势与取舍

优势是实现简单、易测试、不依赖外部密钥系统，并且避免明文凭据落盘。代价是本地 JSON 凭据文件本身仍需要调用方保护，不能满足企业级密钥轮换、权限分级和集中审计需求。Phase 2 先实现本地文件引用，是为了把接口边界固定下来，后续可以把 `CredentialStore` 后端替换成钥匙串或云密钥管理。

## 8. Session 恢复与登录态复用

### 做了什么

Session 模型新增：

- session 状态。
- rolling summary。
- recent observations。
- metadata。
- browser state path。

Browser Agent 会保存 Playwright `storage_state`。使用同一个 `session_id` 的后续任务可以复用登录态。

### 怎么做

`ContextCompressor` 负责把浏览器执行后的关键上下文压缩进 session，例如最近页面类型、URL、observation 摘要和 state path。Browser Agent 启动时如果发现 session 有 state path，会把它交给 Playwright context。

### 为什么这样做

很多真实网页任务不是一次性完成的。没有 session 恢复，每个任务都要重新登录，也无法在人工确认后回到原上下文。保存 `storage_state` 是 Playwright 提供的成熟路径，能以较低复杂度解决登录态复用问题。

### 优势与取舍

优势是实现成本低、恢复效果直接、便于测试。代价是 `storage_state` 可能包含 cookie 和登录 token，必须当作敏感文件管理。Phase 2 没有做加密存储和自动清理，这是后续需要补上的运维能力。

## 9. 审计、Artifact 与可观测性

### 做了什么

Browser Agent 会记录执行过程中的关键事件，并输出：

- 最终任务报告。
- 抽取文本 artifact。
- 页面摘要。
- 失败截图。
- 可选 trace/video。
- action steps 和 observation 摘要。

审计路径中继续应用敏感字段脱敏，登录动作额外避免记录输入值。

### 怎么做

Browser Agent 在动作执行、阻断、失败和完成时写入审计事件。Playwright 工具层负责保存页面级 artifact。任务存储层能反序列化嵌套 dataclass，确保恢复后还能读取执行计划、工具调用和 artifacts。

### 为什么这样做

浏览器自动化失败时，只有最终错误消息通常不够定位问题。截图、page summary 和 steps 能帮助判断是页面识别问题、凭据问题、登录失败、domain 阻断还是风险门禁触发。

### 优势与取舍

优势是问题可回放、可定位、可审计。代价是 artifact 和 trace/video 会增加磁盘占用，也可能保存页面敏感内容。因此这些能力默认保持克制，trace/video 通过显式参数开启。

## 10. Obsidian Vault 契约

### 做了什么

新增 knowledge tool 的配置和占位实现，用于表达 “系统可以从 Obsidian vault 获取知识” 的工具边界。

当前能力包括：

- 配置 vault 路径。
- 检查 vault 可用性。
- 返回结构化 `KnowledgeAnswer`。
- 为后续 sources、confidence、引用路径预留模型。

### 怎么做

`ops_qa` 规划会生成 knowledge tool call。工具实现不做真实 RAG，只返回明确的占位状态和配置检查结果。

### 为什么这样做

Phase 2 的主要风险集中在浏览器自动化和凭据登录。如果同时实现完整知识检索，会引入索引、分块、嵌入、召回、重排和引用可信度等额外变量，影响验收焦点。先固定工具契约，可以让后续 Phase 单独推进知识库质量。

### 优势与取舍

优势是接口先稳定，后续可以替换内部检索实现而不影响 planner 和 controller。代价是当前 knowledge tool 还不能回答真实 vault 内容问题。

## 11. 测试与验收

### 自动化测试

当前测试覆盖：

- 凭据加载成功。
- 缺少凭据引用。
- 缺少用户名或密码。
- 敏感字段脱敏。
- 登录页 planner 动作顺序。
- MFA/验证码阻断。
- 本地 mock 登录成功。
- 密码错误。
- MFA 页面。
- 只读 web_action 回归。
- 远程副作用确认回归。
- inspection 和 ops_qa 既有能力回归。
- session resume 相关路径。

最新自动化结果：

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest
25 passed in 0.26s
```

### 真实浏览器验证

已经使用真实 Chromium 跑过以下验证：

- 动态只读页面任务，确认没有使用固定 actions，实际执行 `open_url -> observe_page -> extract_text -> save_artifact -> finish`。
- 本地 mock 登录页，确认可以输入用户名、密码并提交，随后抽取 dashboard 内容。
- 密码错误场景，确认失败路径和 artifact 输出。
- MFA 页面场景，确认 planner 阻断。
- 登录态复用场景，第一次登录后保存 state，第二次同 session 直接访问 dashboard，不再输入凭据。
- 凭据泄漏检查，确认任务、审计和 artifact 中没有 username/password 明文。

## 12. 主要优势

### 安全边界清楚

系统把只读、本地编辑和远程副作用分开处理。Browser Agent 可以自动完成低风险步骤，但不会静默执行高风险业务提交。

### 凭据暴露面小

任务和 session 只保存 `credential_ref`。真正的 username/password 只在运行时加载，并且不会进入审计和 artifact 的结构化字段。

### 可恢复

pending action、session state、resume URL 和 rolling summary 让人工确认和后续任务可以接在原上下文上继续。

### 可测试

Planner、risk、credentials、Playwright wrapper 和 controller 都有相对独立的边界，可以用单元测试、集成测试和真实浏览器测试分层覆盖。

### 可扩展

后续可以替换 CredentialStore 后端、增强 observation schema、接入 LLM 页面理解、升级 Obsidian RAG，而不需要推翻 Browser Agent 的主循环。

## 13. 关键取舍

### 规则优先，而不是完全 LLM 自主浏览

这样牺牲了一些复杂网页泛化能力，但换来了确定性、安全性和可测试性。Phase 2 的目标是建立可信执行底座，这个取舍是合理的。

### 本地凭据文件，而不是钥匙串或云密钥

这样实现速度快、依赖少、便于本地测试。代价是凭据文件权限和生命周期需要用户或部署环境自己管理。接口已经用 `credential_ref` 隔离，后续替换后端成本较低。

### 保存 `storage_state`，而不是常驻浏览器进程

这样更容易跨任务恢复，也更适合 CLI 工作流。代价是 state 文件本身敏感，需要后续补加密、清理和权限控制。

### 显式确认远程副作用，而不是全自动提交

这样降低误操作风险，适合真实系统接入。代价是某些工作流需要人工介入。Phase 2 优先选择可控性。

### Obsidian 先做契约，不做完整 RAG

这样避免 Phase 2 变成两个大系统同时交付。代价是当前知识问答还不能真正利用 vault 内容，但后续扩展点已经存在。

## 14. 后续建议

1. 为 `storage_state` 和 artifacts 增加过期清理、权限检查和可选加密。
2. 扩展登录错误分类，例如 bad credentials、account locked、MFA required、captcha required、network error。
3. 增加更多真实站点只读任务样例，覆盖不同登录表单结构。
4. 将 CredentialStore 后端抽象到系统钥匙串或云密钥服务。
5. 把 knowledge tool 升级为真实 Obsidian 检索，包含分块、索引、引用和置信度。
6. 在保持风险门禁的前提下，引入更强的页面理解能力，处理复杂表单和多步骤页面。

## 15. 最终评价

Phase 2 当前已经完成从“规划型任务执行器”到“受控浏览器 Agent”的关键跃迁。它能真实打开浏览器，动态观察页面，完成只读抽取，处理账号密码登录，在远程副作用前阻断，并通过 session state 支持恢复和登录态复用。

更重要的是，Phase 2 没有为了演示效果牺牲安全边界。凭据、审计、确认、session 和 artifact 都被纳入同一个执行模型中。这个实现还不是通用网页操作机器人，但已经是一个可以继续扩展的可信底座。
