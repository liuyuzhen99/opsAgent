# Web Agent 调优总结（2026-05-13）

## 背景

本轮调优围绕 opsAgent 的 `web_action` 能力展开，目标是让 web agent 在企业后台类页面中稳定完成“登录、导航菜单、下拉搜索、查询、表格选择、弹窗读取结果”等任务。

实际验证命令主要来自“全球金融管理服务平台”的网银岗位分配场景：

> 登录网站，在左侧侧边栏依次点击网上银行管理，权限管理，网银岗位分配进入对应菜单，等待页面加载完成，然后在授权单位展开授权单位下拉列表，输入“内蒙古伊家好奶酪有限责任公司”，之后点击搜索到的第一个公司，使授权单位处显示为内蒙古伊家好奶酪有限责任公司，之后点击查询按钮，在用户名中输入张越，然后点击下方的查询按钮进行查询，之后选中查询后的第一条数据，点击分配岗位，再点击弹出内容中的已分配岗位，告诉我当前已分配岗位中的岗位名称。

最终真实页面运行结果：任务完成，页面显示已分配岗位 `显示0到0,共0记录`，agent 返回：

> 当前已分配岗位中没有岗位名称。

## 当前 Web Agent 具备的功能

### 1. 单 Session 浏览器执行与人工确认恢复

web agent 现在支持在一个浏览器实例中持续执行任务。遇到需要人工确认的动作时，浏览器页面不会关闭，确认后从当前页面和已执行步骤继续。

实现位置：

- `src/aiops_agent/browser/agent.py`
- `src/aiops_agent/agent/controller.py`
- `src/aiops_agent/chat.py`

核心实现：

- `BrowserAgentTool` 内部维护 `_active_tools`，key 为 `session_id:task_id`。
- 遇到 `awaiting_confirmation` 时缓存当前 `PlaywrightBrowserTool`，并设置 `keep_browser_open=True`，避免 `finally` 中关闭浏览器。
- `AgentController.confirm()` 将 `confirmed_action`、`replay_actions`、`prior_steps`、`session_state_path` 写回工具参数。
- 恢复时优先复用活动浏览器实例，并带上 `prior_steps`，避免从头重放。
- Chat 模式 `_handle_confirmation()` 使用循环处理连续多次确认，第二次、第三次确认都会继续提示并恢复执行。

为什么这样实现：

- 真实企业后台页面有大量前端状态、弹层、iframe、select2 下拉和登录态，仅靠 URL/session storage 重建页面容易丢状态。
- 复用浏览器实例比“确认后重新打开页面并重放动作”更稳定，尤其适合当前这种需要人工确认但又不希望页面被重置的 RPA 场景。
- `prior_steps` 保留历史执行记录，让 planner 和稳定器知道哪些动作已经成功，避免确认后重复执行同一动作。

### 2. 动作稳定器：校验 LLM 动作是否符合用户意图

web agent 现在不是直接执行 LLM 返回的动作，而是在执行前经过 `_stabilize_action()` 修正。

实现位置：

- `src/aiops_agent/browser/agent.py`

核心能力：

- 修正输入字段漂移：例如用户说“在用户名/用户名称中输入张越”，LLM 如果选择了“登录名称”，稳定器会根据用户原始命令和当前页面元素改回“用户名/用户名称”。
- 修正输入后的下一步点击：例如用户说“输入公司名，之后点击搜索到的第一个公司”，即使公司名在后文重复出现，也只取最近的“之后点击...”作为下一步目标。
- 处理“搜索到的第一个公司”：将它稳定成上一步输入的公司名称，避免错点默认公司或原 select 控件。
- 处理“选中查询后的第一条数据”：当 LLM 准备点击“分配岗位”但页面上已有查询结果表格时，稳定器会先插入“点击第一条数据”动作。
- 避免重复选中第一条：如果第一条数据已经被稳定器选中过，后续不会再次插入相同动作。

为什么这样实现：

- LLM 很擅长理解任务，但在复杂 DOM 中容易把相邻字段、同名按钮、重复文本搞混。
- 这些错误不是“模型不知道”，而是企业后台页面的局部歧义太强，因此更适合用确定性规则做最后一层动作校验。
- 稳定器只修正高频、可判定的动作，不替代 LLM 做完整规划，能降低误修正风险。

### 3. 每步执行后的反思与提前终止

每个动作执行后，agent 会生成本地 `reflection`，判断：

- 是否符合用户意图
- 执行失败原因是什么
- 是系统缺少信息，还是定位失败、登录失败、网站不可用等其他问题
- 是否应该继续、修正或提前终止

实现位置：

- `src/aiops_agent/browser/agent.py`

每个 step 现在会包含类似字段：

```json
{
  "reflection": {
    "intent_aligned": true,
    "intent_reason": "输入动作目标与用户要求的字段 用户名 一致。",
    "failure_category": "none",
    "failure_reason": "",
    "terminal": false,
    "terminal_reason": null,
    "next_decision": "continue"
  }
}
```

提前终止场景：

- 登录失败或仍停留在登录页：返回明确登录失败原因。
- 找不到菜单：返回 `系统中没有找到对应菜单：xxx。`
- 找不到公司/下拉选项：返回 `系统中没有找到对应公司：xxx。`
- 查询结果为空：返回 `系统中没有找到符合条件的信息：字段=值。`
- 规划动作与用户意图冲突：执行前阻断，避免误点。

为什么这样实现：

- 之前 agent 会把“系统没有数据”误当成“下一步还没找到”，继续循环直到最大步数。
- 企业后台任务中，“菜单不存在、公司不存在、查询无结果”通常是业务结论，不是需要继续探索的中间状态。
- 将反思放在执行器层面，而不是只靠 LLM 自评，可以保证稳定、可测试、可审计。

### 4. Select2/下拉搜索/弹层选项支持

针对授权单位这种 select2 风格下拉控件，Playwright 定位逻辑做了增强。

实现位置：

- `src/aiops_agent/browser/playwright_tool.py`

核心能力：

- 输入动作会优先识别当前聚焦的搜索框、弹层内搜索框、select2/element/ant 风格下拉中的可编辑输入。
- 点击公司名时，会优先在可见弹层中查找匹配选项，而不是再次点击原始 select 控件。
- 对 select2 mask 或 overlay 拦截点击的情况，增加 popup fallback。
- 避免把 `select2-search input` 误判为裸 CSS selector；真正的 CSS selector 仍然支持，例如 `input[name='userName']`。

为什么这样实现：

- select2 控件打开后，真正可输入的搜索框往往不在原始 select 节点里，而是在全局浮层中。
- LLM 只能看到语义信息，不一定知道弹层 DOM 的真实结构。
- 在 Playwright 层做弹层感知，能复用到其它类似 UI 组件。

### 5. 表格第一行选择与 jqGrid 支持

agent 现在能更稳定地执行“选中查询后的第一条数据”。

实现位置：

- `src/aiops_agent/browser/agent.py`
- `src/aiops_agent/browser/playwright_tool.py`

核心能力：

- 稳定器会在“点击分配岗位”前检查用户是否要求先选中第一条数据。
- Playwright 定位会优先找 jqGrid 数据行、第一行 checkbox/radio、`tr.jqgrow`、带数据链接的第一行。
- 避免把表头当成第一行数据。
- 如果页面上方 label 或查询区域遮挡了实际行，普通 pointer click 被拦截时，会对第一行 fallback 到 `dispatch_event("click")`。

为什么这样实现：

- 企业后台表格经常使用 jqGrid 这种旧式组件，DOM 结构不等同于语义化 table。
- “第一条数据”不是一个可见按钮，LLM 很难稳定产出正确 selector。
- 在执行层统一识别数据行，比让 LLM 猜 DOM 更可靠。

### 6. 用户查询结果与岗位名称答案提取

agent 现在不会在没有答案时假成功。

实现位置：

- `src/aiops_agent/browser/agent.py`

核心能力：

- 如果用户命令包含“告诉我、返回、输出、当前、是什么、岗位名称”等答案要求，最终必须能从页面提取答案。
- 对“已分配岗位”页签做了专门解析：
  - `显示0到0,共0记录` -> `当前已分配岗位中没有岗位名称。`
  - 有岗位名称列表 -> 返回 `当前已分配岗位中的岗位名称：xxx、yyy。`
- 仍保留通用列匹配能力，例如按“用户名称”查询并提取“登录名称”。

为什么这样实现：

- “完成页面动作”和“回答用户问题”不是一回事。
- read-only 查询任务的成功标准应该是拿到用户要求的信息，而不是仅仅点完按钮。
- 对高频业务结果做结构化解析，可以减少 LLM 总结时的幻觉。

### 7. 风险评估与确认策略调整

风险评估现在更贴近浏览器任务本身。

实现位置：

- `src/aiops_agent/browser/risk.py`

调整点：

- `授权` 不再作为单独 unsafe 关键词，否则“授权单位”字段会被误判为远端写入。
- `press` 被归为 `safe_local_edit`。
- 真正具有远端副作用的词仍然需要确认，例如保存、删除、创建、开通、撤销、提交等。

为什么这样实现：

- 企业系统里“授权单位”是常见字段名，不代表执行授权变更。
- 风险判断需要结合动作类型和语义，不能只做粗暴关键词匹配。
- 误判会导致确认时机不一致，用户体验和执行稳定性都会变差。

### 8. LLM Browser Planner 提示词增强

LLM 提示词也做了约束，降低源头规划错误。

实现位置：

- `src/aiops_agent/llm/langchain_provider.py`

新增约束包括：

- 输入或选择前必须校验字段标签，不要混淆“用户名称”和“登录名称”。
- 尊重用户命令中的显式顺序。
- 弹窗打开时优先操作弹窗内控件。
- searchable dropdown/select2：打开后输入搜索框，再点击匹配选项，不要重复点击原 select。
- 第一行/第一条结果优先选择第一条数据行的 checkbox/radio。
- read-only 查询任务必须在拿到答案后 finish。

为什么这样实现：

- 提示词可以减少 LLM 的初始错误。
- 但提示词不是唯一保障，最终仍由稳定器和执行器反思兜底。
- 这种“LLM 规划 + 确定性校验 + DOM 执行增强”的组合，比单纯加 prompt 更可控。

### 9. AgentSkills.io 格式的 Web Skill 沉淀与复用

web agent 现在支持在 chat 中通过 `/save-skill [name]` 将最近一次成功的 `web_action` 沉淀为 AgentSkills.io 风格的 skill 目录。

实现位置：

- `src/aiops_agent/browser/skills/`
- `src/aiops_agent/chat.py`
- `src/aiops_agent/agent/controller.py`
- `src/aiops_agent/planning.py`
- `src/aiops_agent/browser/agent.py`
- `src/aiops_agent/cli.py`

生成目录：

```text
storage/web_skills/{skill-name}/
  SKILL.md
  assets/workflow.json
  references/notes.md
```

核心能力：

- `/save-skill` 只从当前 session 最近一次成功的 `web_action` 生成。
- 最近成功任务通过 session metadata 中的 `browser_last_success_task_id` 定位。
- 生成前校验：
  - `task.intent == web_action`
  - `task.status == success`
  - `result.data.status == completed`
  - `result.data.steps` 非空
  - 成功路径中没有 terminal/stop 型 reflection
  - 查询类任务必须有明确 answer
- `SKILL.md` frontmatter 遵守 AgentSkills.io 基本要求：
  - `name` 与目录名一致
  - `description` 非空
  - `name` 只允许小写字母、数字、连字符，不允许首尾连字符或连续连字符，长度不超过 64
  - `metadata` 值统一为字符串
- `assets/workflow.json` 保存 opsAgent 可执行的参数化动作序列。
- `references/notes.md` 保存页面结构、参数说明、失败处理和答案提取说明。
- 登录动作和密码不会进入 skill；登录仍走现有 `CredentialStore`、`credential_ref`、`login_fields`。
- 用户输入值会被参数化，例如 `username`、`company_name`、`role`、`email`。
- 菜单、按钮、字段名、页签等稳定 UI 文本会保留为固定动作目标。
- 后续 `web_action` 会在规划阶段扫描 `storage/web_skills/*/SKILL.md`。
- 命中同站点、关键词、字段、参数条件后，将 workflow 渲染为 `auto_plan=False` 的固定动作执行。
- skill 固定动作遇到业务缺失（菜单不存在、公司不存在、查询无数据、登录失败、站点不可用）直接提前终止。
- 非业务定位/执行失败允许回退现有 LLM planner 一次。

为什么这样实现：

- 项目里原本已经有 `auto_plan=False + actions` 的固定动作执行通道，不需要另起一套执行器。
- AgentSkills.io 的 `SKILL.md` 适合给人和跨 agent 阅读；真正机器可执行的细节放进 `assets/workflow.json` 更稳定。
- 将 skill 匹配放在 `PlanningService` 阶段，而不是塞进 `BrowserAgentTool`，能保持“规划负责选择策略，执行器负责执行动作”的边界。
- 同站点硬过滤可以避免不同系统里相似中文描述误命中。
- 参数化动作能复用成功路径，同时避免把真实用户输入、密码、截图、cookie、token 等敏感信息落盘。

使用方式：

```text
/save-skill
/save-skill ifinance-query-role
```

生成后会打印：

```text
已生成 skill: storage/web_skills/{skill-name}
参数: username, role
动作数: 8
匹配关键词: 用户管理, 查询, 岗位名称
```

### 10. 无参数启动时自动识别站点、凭据与默认 Web Agent 参数

web agent 现在可以用更短的命令启动：

```bash
.venv/bin/python -m aiops_agent chat
```

然后在 chat 中输入：

```text
登录ifinance网站
```

系统会自动从配置中识别并补齐运行参数。

实现位置：

- `src/aiops_agent/browser/credentials.py`
- `src/aiops_agent/agent/controller.py`
- `src/aiops_agent/cli.py`
- `src/aiops_agent/chat.py`

核心能力：

- 默认读取 `configs/browser_sites.json`。
- 自然语言中出现 site key 时自动匹配，例如 `ifinance`。
- 匹配站点后自动写入：
  - `site_key`
  - `site_config`
  - `start_url`
  - `allowed_domains`
  - `requires_login`
- 默认读取 `configs/credentials.local.json`，也支持环境变量 `AIOPS_BROWSER_CREDENTIAL_CONFIG` 覆盖。
- 自动为站点推断 credential ref，优先顺序：
  - `{site_key}`
  - `{site_key}_admin`
  - `{site_key}-admin`
  - 如果只有一个以 `{site_key}_` 或 `{site_key}-` 开头的凭据，也会使用它
- chat/run 默认执行步数改为 `40`。
- 默认 `browser_slow_mo_ms` 改为 `300`。
- 显式 CLI 参数仍然可以覆盖默认值。

为什么这样实现：

- 用户实际使用时经常只想说“登录某个网站并执行任务”，不应该每次重复输入站点配置、凭据配置、凭据 ref、步数和 slow-mo。
- `browser_sites.json` 已经是站点能力的唯一配置源，自然语言里的 site key 可以安全映射到它。
- `credentials.local.json` 是本地敏感配置，默认只在文件存在时读取；不存在则不影响普通启动。
- 凭据 ref 采用约定优先而不是盲猜密码内容，降低误用风险。

## 实施过程中遇到的问题与解决办法

### 问题 1：人工确认后浏览器页面被关闭，任务从头开始

现象：

- 第一次需要确认时，任务进入 `awaiting_confirmation`。
- 用户输入 `y` 后，浏览器上下文已关闭，恢复只能重新打开页面或重放动作。
- 页面状态、弹层状态、登录后上下文容易丢失。

解决：

- 在 `BrowserAgentTool` 中缓存活动 `PlaywrightBrowserTool`。
- awaiting confirmation 时不关闭浏览器。
- confirm 时携带 `prior_steps` 并复用活动工具实例。

结果：

- 确认后从原页面继续。
- 不再因为确认打断而重新登录或回到起点。

### 问题 2：第二次确认时不再弹提示，agent 停住

现象：

- 第一次确认后继续执行。
- 如果后续动作再次需要确认，任务虽然回到 `awaiting_confirmation`，但 chat runner 没有继续提示。

解决：

- `ChatRunner._handle_confirmation()` 改为 while 循环。
- 每次 confirm 返回后，如果状态仍是 `awaiting_confirmation`，继续打印确认摘要并等待用户输入。

结果：

- 多次确认可以串行完成，不会卡在第二次确认。

### 问题 3：同一动作重复确认或确认后看不到动作执行

现象：

- 确认后 agent 可能再次准备同一个动作。
- 用户看到“确认了”，但浏览器无明显变化。

原因：

- 恢复时没有完整携带已执行 steps。
- `confirmed_action` 与 replay 动作之间缺少“是否已经执行过”的判断。

解决：

- confirm 参数中增加 `prior_steps`。
- `_next_action()` 用 `_action_already_executed()` 判断确认动作是否已成功执行过。

结果：

- 已确认且执行成功的动作不会再次重复。

### 问题 4：“授权单位”被误判成高风险，确认时机不一致

现象：

- 有时点击“授权单位”需要确认，有时不需要。
- 实际这是一个下拉字段，不是授权变更动作。

解决：

- 从 unsafe hints 中移除单独的“授权”。
- 保留真正写入动作关键词，如提交、保存、创建、删除、开通、撤销等。

结果：

- 查询类动作的确认时机更稳定。

### 问题 5：Select2 下拉中输入公司名失败

现象：

- agent 试图 `fill` 授权单位 label 或原 select 控件。
- 报错类似 `Locator.fill: Timeout ... get_by_label("授权单位")`。

解决：

- 输入动作增加弹层搜索框定位：
  - focused input
  - visible popup input
  - select2/dropdown/popover 内 input
- 不把 `select2-search input` 当作普通 CSS selector。

结果：

- 下拉展开后能输入公司名。

### 问题 6：“点击搜索到的第一个公司”没有执行，反而点了后面的查询

现象：

- 用户命令中公司名重复出现：
  - 输入公司名
  - 点击第一个公司
  - 使授权单位显示为公司名
  - 再点击查询
- 原逻辑根据文本 anchor 找下一步时，可能跳到后面的“点击查询”。

解决：

- `_expected_click_after_last_type()` 只取上一步输入值后最近的 `点击...` 片段。
- `_means_first_search_result()` 识别“搜索到的第一个公司”。
- 稳定器将该动作目标改成上一步输入的公司名。

结果：

- 会先点击公司下拉结果，再点击查询。

### 问题 7：公司选项点击被 select2 mask 拦截

现象：

- Playwright 报错：
  - `select2-drop-mask intercepts pointer events`
- 目标 locator 指向原 select，而不是弹层选项。

解决：

- 点击时优先查找可见 popup/listbox 内的 option。
- 原 locator 点击失败时 fallback 到 popup option locator。

结果：

- 能正确点击弹层中的目标公司。

### 问题 8：“用户名”输入到了“登录名称”

现象：

- 用户说“在用户名中输入张越”。
- LLM 选择了“登录名称”输入框。

解决：

- 稳定器从用户原始命令中提取“值 -> 字段”的约束。
- 当前页面中如果存在精确字段“用户名/用户名称”，就改用该元素。
- Playwright 层也增加 `用户名/用户名称` 与 `登录名称` 的语义 selector 区分。

结果：

- 张越会输入到用户名字段，而不是登录名称。

### 问题 9：没有选中查询后的第一条数据

现象：

- LLM 直接点击“分配岗位”，跳过“选中查询后的第一条数据”。

解决：

- 稳定器在“准备点击分配岗位”前检查：
  - 用户命令是否包含选中第一条/第一个/首个。
  - 当前页面是否有用户查询结果表格。
  - 是否已经选中过第一条。
- 若满足条件，插入 `click -> 第一条数据`。

结果：

- 会先选中查询结果第一条，再点击分配岗位。

### 问题 10：第一行点击被页面元素遮挡

现象：

- 表格行可见，但 Playwright pointer click 被上层 label/query panel 拦截。

解决：

- 第一行点击失败且目标语义为“第一条/第一个”时，fallback 到 `dispatch_event("click")`。

结果：

- 能完成 jqGrid 第一行选中。

### 问题 11：已分配岗位为空时没有明确答案

现象：

- 弹窗中“已分配岗位”页签显示 `显示0到0,共0记录`。
- agent 可能不知道如何回答“岗位名称”。

解决：

- 增加 `_assigned_role_name_answer()`。
- 对空记录返回明确中文答案。
- 对非空记录提取岗位名称列表。

结果：

- 能回答 `当前已分配岗位中没有岗位名称。`

### 问题 12：系统没有菜单/公司/数据时循环到最大步数

现象：

- 登录有问题、菜单不存在、公司不存在、查询无结果时，agent 会继续让 LLM 尝试下一步，直到达到最大浏览器步骤预算。

解决：

- 增加每步 `_reflect_after_action()`。
- 增加 `_early_stop_reason()`。
- 根据失败类型和页面信号分类：
  - `login_failure`
  - `site_unavailable`
  - `system_missing_information`
  - `locator_failure`
  - `execution_failure`
- 对业务缺失信息直接返回用户可读结论。

结果：

- 不再把“系统没有数据”当成可恢复失败反复尝试。
- 任务会提前结束并说明原因。

### 问题 13：成功路径无法沉淀复用，只能每次重新规划

现象：

- 某些企业后台流程一旦成功跑通，其实后续高度相似。
- 但之前每次都要重新依赖 LLM planner 从页面 observation 中推下一步。
- 同样的菜单、字段、下拉、查询、页签步骤会重复暴露在 LLM 的不确定性里。

解决：

- 新增 `browser.skills` 模块。
- 在 chat 中增加 `/save-skill [name]` 命令。
- 从最近一次成功 `web_action` 的 `result.data.steps` 生成 AgentSkills.io 风格目录。
- 将动作参数化后保存到 `assets/workflow.json`。
- 在 `PlanningService` 中扫描并匹配 `storage/web_skills`，命中后渲染成固定 `BrowserAction` 列表。

结果：

- 成功跑通的流程可以沉淀为可读、可审计、可复用的 skill。
- 相似任务优先走确定性 workflow。
- 非业务失败仍可回退 LLM planner 一次。

### 问题 14：启动 web agent 时需要反复输入站点和凭据参数

现象：

- 访问 ifinance 站点时需要使用较长命令：

```bash
.venv/bin/python -m aiops_agent chat \
  --browser-site ifinance \
  --browser-sites-config configs/browser_sites.json \
  --credential-config configs/credentials.local.json \
  --credential-ref ifinance_admin \
  --headed \
  --browser-slow-mo 300 \
  --max-steps 40
```

- 用户希望只运行 `aiops_agent chat`，然后在自然语言里说“登录ifinance网站”。

解决：

- `CredentialStore` 默认在存在时加载 `configs/credentials.local.json`。
- `AgentController` 在 intent parse 后根据用户文本匹配 `browser_sites.json` 中的 site key。
- 匹配站点后自动补齐站点 runtime config、入口 URL、允许域名和登录需求。
- 根据 site key 自动推断默认 credential ref。
- 将 chat/run 默认 `max_steps` 调整为 40，默认 `browser_slow_mo_ms` 调整为 300。

结果：

- 可以直接用 `.venv/bin/python -m aiops_agent chat` 启动。
- 输入“登录ifinance网站”即可自动使用 `ifinance` 站点配置和 `ifinance_admin` 凭据。
- 显式参数仍可覆盖默认行为。

## 当前实现的整体架构

当前 web agent 执行链可以概括为：

```text
用户自然语言任务
  -> Intent/Plan 生成 web_action
  -> 若自然语言包含 browser_sites.json 中的 site_key，则补齐站点、凭据、入口 URL、允许域名
  -> PlanningService 扫描 storage/web_skills 并尝试匹配 AgentSkills.io skill
  -> skill 命中：渲染 assets/workflow.json 为固定 BrowserAction 列表，auto_plan=False
  -> skill 未命中：进入现有 LLM/规则 planner
  -> BrowserPlanner 基于页面 observation 产出下一步动作
  -> BrowserAgent 稳定器校验/修正动作
  -> RiskEvaluator 判断是否需要人工确认
  -> PlaywrightBrowserTool 执行动作
  -> observe 页面状态
  -> action reflection 判断是否符合意图、是否失败、是否应停止
  -> 若继续，进入下一轮；若缺信息/失败不可恢复，提前 blocked；若完成，提取答案
```

选择这种架构的原因：

- LLM 负责理解自然语言和动态规划，适合处理页面变化和任务语义。
- 稳定器负责确定性纠偏，适合处理字段名、按钮顺序、第一行、下拉选项等可规则化问题。
- Playwright 工具层负责 DOM 细节，适合封装 select2、jqGrid、popup、iframe、overlay 等 UI 技术细节。
- 执行后反思负责收束错误，避免无限循环和最大步数兜底。
- 风险评估单独处理，避免安全策略混进业务规划逻辑。
- Web Skill 负责复用已经验证过的成功路径，降低相似任务对 LLM 动态规划的依赖。
- 自然语言站点识别负责把“登录 ifinance 网站”转成受控站点配置，而不是让用户每次手动传 CLI 参数。

## 测试覆盖

新增/增强测试主要覆盖：

- 确认后复用浏览器实例，不关闭页面。
- 连续多次人工确认。
- 授权单位不再被误判为 unsafe。
- select2/searchable dropdown 输入和选项点击。
- popup mask 拦截 fallback。
- 用户名/用户名称与登录名称区分。
- 输入后下一步点击顺序修正。
- 搜索到的第一个公司修正为输入公司名。
- 查询结果第一条数据选择。
- jqGrid 第一行而非表头。
- 第一行点击被遮挡时 dispatch fallback。
- 已分配岗位为空/非空答案提取。
- 每步 reflection 记录。
- 缺菜单、缺公司、查无数据时提前终止。
- LLM 动作与用户意图冲突时执行前阻断。
- `/save-skill` 从最近成功 web_action 生成 AgentSkills.io 风格目录。
- `SKILL.md` name/frontmatter/metadata/workflow schema 校验。
- workflow 动作参数化，真实用户输入和敏感信息不落盘。
- 同站点 skill 匹配与不同站点不误命中。
- skill 命中后规划阶段输出 `auto_plan=False` 和固定 actions。
- chat `/save-skill [name]` 命令输出路径、参数、动作数、匹配关键词。
- 默认加载 `configs/credentials.local.json`。
- 根据 site key 自动推断 `credential_ref`。
- 自然语言“登录ifinance网站”自动匹配 `browser_sites.json` 中的站点配置。
- chat/run 默认 `max_steps=40`、`browser_slow_mo_ms=300`。

最近一次全量测试结果：

```text
118 passed, 6 skipped, 1 warning
```

## 后续建议

1. 将 `reflection` 暴露到执行报告中，便于用户排查“为什么提前终止”。
2. 对更多企业后台组件增加执行层适配，例如 zTree、layui table、easyUI combobox。
3. 对“系统中没有找到...”类结论增加标准化错误码，方便上层 CLI 或 UI 做分类展示。
4. 将真实页面的关键 observation 脱敏沉淀为回归样例，减少只能靠线上页面验证的问题。
5. 对菜单导航建立轻量页面状态机，进一步降低 LLM 在多级菜单中的探索成本。
6. 为 `assets/workflow.json` 增加更严格的 JSON Schema 文件，便于独立校验和版本演进。
7. 对 web skill 增加导入/导出和列表命令，例如 `/skills`、`/delete-skill`。
