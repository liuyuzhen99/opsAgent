# Web Agent Init Review Report - 2026-05-08

## 1. 背景与目标

本轮工作的起点是：opsAgent 已经具备一个简单的 Web Agent 雏形，但它更接近“规则 planner + 固定 workflow + Playwright 受控动作”的实现。用户期望的是更接近真实 Web Agent 的能力：

1. 能登录目标站点 `http://10.60.143.163:7001/ifinance-portal/login`。
2. 能在登录后根据自然语言完成用户查询、用户创建等后台操作。
3. 不依赖提前写死的业务 workflow。
4. 能自动获取页面 DOM，理解输入框、按钮、菜单、表格和页面文本。
5. 能以 LLM ReAct 方式循环执行：观察页面、分析下一步、执行动作、再观察。
6. 对真实远端写操作保持确认门禁，不让 Agent 未确认提交。

这次迭代的方向从“固定后台系统 workflow 自动化”转为“LLM 驱动的受控浏览器 ReAct”。底层 Playwright 动作仍然受限，凭据仍然本地注入，风险控制仍然存在，但页面控件选择和后续导航开始交给 LLM 基于 DOM observation 决策。

## 2. 初始能力评估

### 2.1 当时已有能力

原有 Web Agent 已经有以下基础：

- `web_action` 意图识别。
- `BrowserAgentTool` 执行循环。
- `BrowserPlanner` 规则规划。
- `PlaywrightBrowserTool` 执行受控动作。
- `CredentialStore` 通过 `credential_ref` 注入登录账号密码。
- `RiskEvaluator` 在提交、保存、删除、授权等远端写动作前阻断。
- artifact 输出，包括截图、页面摘要、execution report、trace/video 可选开关。
- 站点配置 `browser_sites.json` 和账号/权限 workflow。

### 2.2 关键缺口

当时并没有真正的浏览器 ReAct 能力。

虽然仓库里已有 `BrowserPlannerOutput` 这样的 LLM 动作 schema，但它只是校验模型，没有接入主执行路径。实际执行仍主要依赖：

- 规则 planner。
- 固定 workflow。
- 简单语义定位，例如“用户名”“密码”“登录”。

因此它不能可靠完成：

- 基于页面 DOM 自主判断用户名框、密码框、登录按钮。
- 登录后根据自然语言寻找菜单、搜索框、查询按钮和结果区域。
- 从页面文本中判断答案是否已经出现。
- 在复杂后台页面中进行多轮观察和动作修正。

## 3. 实现路线调整

### 3.1 第一阶段：为 iFinance 场景补齐配置能力

最初为了快速进入真实站点测试，增加了 iFinance 站点配置：

- `site_key`: `ifinance`
- `base_url`: `http://10.60.143.163:7001`
- `login_url`: `http://10.60.143.163:7001/ifinance-portal/login`
- `allowed_domains`: `10.60.143.163:7001`
- `login_fields`
- `search_user`
- `create_user`

同时增加了 `credentials.example.json`，说明本地凭据文件结构。

这一阶段仍偏 workflow 方案，目的是先跑通真实站点入口和凭据注入。

### 3.2 第二阶段：补入查询用户工作流

新增了 `search_user` 工作流，支持自然语言中的：

- 查询用户
- 搜索用户
- 查找用户
- search user
- find user

并让只读查询不会被错误标记为远端写操作。

当时发现一个风险误判问题：查询动作的 `expected_outcome` 中使用了“提交用户查询”，触发了 `RiskEvaluator` 里的“提交”风险词。后来改为“执行用户查询并刷新结果列表”，避免只读查询被误拦。

### 3.3 第三阶段：明确转向 LLM ReAct

用户指出固定 workflow 不是目标能力，希望 Web Agent 能自动分析 DOM，并用 ReAct 框架完成网页动作。

于是调整主路径：

```text
open_url
observe_page
LLM 根据 DOM observation 和用户目标选择一个动作
Playwright 执行动作
observe_page
LLM 再选择下一步
...
finish / extract_text / save_artifact
```

此后自然语言 web 请求不再绑定 `search_user/create_user/...` workflow。workflow 代码和测试暂时保留，作为未来确定性流程能力备用，但当前重点是让 ReAct 跑顺、跑稳。

## 4. LLM ReAct 接入

### 4.1 新增浏览器动作规划接口

在 `BaseLLMProvider` 中新增：

```python
plan_browser_action(
    goal,
    observation,
    steps,
    allowed_domains,
    success_criteria,
    forbidden_actions,
) -> BrowserAction
```

`LangChainLLMProvider` 实现该接口，并把以下内容发给 LLM：

- 用户目标。
- 当前 URL。
- 页面标题。
- 页面类型。
- 可见消息。
- 表单字段。
- 可交互 DOM 元素。
- 页面正文摘要。
- 最近若干步历史动作。
- 允许域名。
- 成功标准。
- 禁止动作。

LLM 返回结构化 JSON：

```json
{
  "thought": "当前页面有用户名输入框、密码输入框和登录按钮，下一步填写用户名。",
  "action": {
    "type": "type_username",
    "target_id": "aiops-el-0",
    "target_hint": "username",
    "value": null,
    "expected_outcome": "用户名输入框被填充",
    "timeout_ms": 5000
  }
}
```

### 4.2 扩展 LLM 动作协议

原 LLM 动作只支持通用动作：

- `open_url`
- `click`
- `type`
- `select`
- `wait_for`
- `observe_page`
- `save_artifact`
- `finish`

本轮扩展了登录专用动作：

- `type_username`
- `type_password`
- `login_submit`
- `press`
- `extract_text`

这样 LLM 可以选择“这是用户名框”“这是密码框”“这是登录按钮”，但真实用户名和密码仍由本地凭据注入。

### 4.3 凭据安全设计

登录页也走 ReAct，但 LLM 不接触真实密码。

实际流程是：

1. LLM 看到 DOM observation。
2. LLM 输出 `type_username`，并指定 `target_id` 或 `target_hint`。
3. Browser Planner 在本地把 `credential_username` 注入 action value。
4. LLM 输出 `type_password`。
5. Browser Planner 在本地把 `credential_password` 注入 action value。
6. 审计、artifact、execution report 中仍对账号密码做脱敏。

这样既保留 LLM 的 DOM 理解能力，又不把凭据暴露给模型。

## 5. DOM Observation 增强

### 5.1 元素可见性过滤

真实 iFinance 登录页里存在多个 password input：

- `spa`: 可见登录密码框。
- `pinno`: 不可见证书密码框。

早期定位逻辑会把“密码”匹配到不可见的 `pinno`，导致：

```text
Locator.fill: Timeout 5000ms exceeded
element is not visible
```

为此调整了 DOM 采集逻辑：

- 只采集可见、display 非 none、visibility 非 hidden、尺寸大于 0 的元素。
- locator 选择时优先可见且 enabled 的元素。
- 对 `css=` / `xpath=` selector 也使用可用元素优先策略。

### 5.2 target_id 唯一性修复

真实测试中发现 `data-aiops-id` 会重复，例如多个元素都出现 `aiops-el-13`。这会导致 LLM 选择了一个元素，但执行时点击到另一个元素。

原因是页面 DOM 多次局部刷新后，旧元素保留了已注入的 `data-aiops-id`，新的 observation 又按局部 index 生成 ID，最终出现冲突。

修复方式：

- 每次 observation 时，对当前可交互元素重新顺序写入 `data-aiops-id`。
- 当前 observation 内保证 `aiops-el-N` 唯一。
- execution 使用 `target_id` 时更可靠。

### 5.3 保留 target_id 执行

真实 execution report 中发现：

```json
{
  "type": "login_submit",
  "target_hint": "登录",
  "target_id": "aiops-el-2"
}
```

LLM 已经选中了 `target_id=aiops-el-2`，但 `_login_submit` 内部构造 click action 时丢掉了 `target_id`，只保留 `target_hint`，导致执行器重新按文本“登录”定位。

修复：

- `_login_submit` 构造内部 click action 时保留 `target_id`。
- 这样 LLM 选中的精确 DOM 元素会被实际点击。

### 5.4 增加页面正文和元素上下文

原 observation 只给 LLM 可交互元素列表，缺少页面正文和元素周边语义。登录后 iFinance 页面中有大量菜单，仅靠链接文本容易误判。

本轮新增：

- `BrowserObservation.page_text`
- `InteractiveElement.title`
- `InteractiveElement.href`
- `InteractiveElement.placeholder`
- `InteractiveElement.context`

LLM prompt 中现在会看到：

- 页面正文前若干字符。
- 每个元素所在 form/li/tr/div 等容器的周边文本。
- 可交互元素本身的 name/text/title/href/placeholder。

真实测试中已经能看到页面正文包含：

```text
您好，高斌 ！
系统状态 ： 开机
客户信息管理
客户信息维护
查询
内部客户信息查询
```

这为后续判断“答案已出现在页面上”或“应进入哪个模块”提供了更好的上下文。

## 6. 登录行为修复

### 6.1 登录按钮是否点击

用户怀疑 agent 可能没有点击登录按钮。

通过 execution report 验证，确实执行了：

```text
type_username
type_password
login_submit
```

其中 `login_submit` 的目标为：

```text
target_id=aiops-el-2
target_hint=登录
```

因此当时报错不是“没有做登录动作”，而是“登录提交后仍被观测为登录页”。

### 6.2 登录等待时间

iFinance 点击登录后跳转较慢，原逻辑过早判断失败。修复为：

- `login_submit` 的等待时间至少 30 秒。
- 点击后等待 DOMContentLoaded。
- 继续等待 URL 变化或登录页可见 password input 消失。
- 若没有明确错误，不立即关闭浏览器，而是允许 ReAct 继续 `wait_for/observe_page`。

### 6.3 登录失败信号

为了避免错误密码也被当成“慢加载”，新增失败信号判断：

- 错误
- 失败
- 无效
- 不正确
- 密码错误
- 账号不存在
- incorrect
- invalid
- failed
- error

如果登录页出现明确失败消息，则立即 block；如果只是仍在登录页但没有失败提示，则继续等待/观察。

## 7. ReAct 执行路径验证

### 7.1 本地依赖和 venv

按用户要求改用项目内 `.venv`：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e . pytest
```

并将 `.venv/` 加入 `.gitignore`。

Playwright 自带 Chromium 下载过程中遇到卡住问题，后续真实测试改用本机 Chrome：

```bash
--browser-channel chrome
```

### 7.2 测试结果

本轮多次运行全量测试，最终结果：

```text
44 passed, 1 warning
```

warning 来自 `langgraph` 的 pending deprecation，不影响当前功能。

### 7.3 真实站点测试

使用命令类似：

```bash
.venv/bin/python -m aiops_agent run \
  "登录网站，查询用户名称为高斌的用户的登录名称是什么" \
  --browser-site ifinance \
  --browser-sites-config configs/browser_sites.json \
  --credential-config configs/credentials.local.json \
  --credential-ref ifinance_admin \
  --browser-channel chrome \
  --headed \
  --browser-slow-mo 200 \
  --browser-trace \
  --max-steps 60
```

真实执行观察到：

1. ReAct 正确识别登录页控件。
2. 成功填写用户名和密码。
3. 执行 `login_submit`。
4. 登录后进入业务页面，而不是停留在登录页。
5. 页面正文出现 `您好，高斌！`。
6. 后续 ReAct 开始根据菜单继续探索。

这说明登录链路已从“固定猜测控件”推进到“LLM 基于 DOM 选择控件并完成登录”。

## 8. 发现的问题与修复

### 8.1 `max_steps` 参数未生效

真实测试时用户传了 `--max-steps 40`，但 browser agent 仍在 20 步停止。

原因：

- `PlanningService` 已经把 `max_steps` 写入 tool call params。
- `AgentController._tool_execute_node` 使用 `setdefault("max_steps", task.max_steps)`。
- 因为 params 中已有默认 20，`setdefault` 不覆盖。

修复：

```python
call_spec.params["max_steps"] = task.max_steps
```

现在 CLI 的 `--max-steps` 会真实传入 browser agent。

### 8.2 过早关闭浏览器

原逻辑在 `login_submit` 后只要仍是登录页就立即 block 并关闭浏览器。对于慢站点，这会造成“还没做完就关闭”的体验。

修复：

- 登录后无明确失败信号时继续等待/观察。
- 仅在明确失败或多次仍不能离开登录态时 block。

### 8.3 ReAct 重复 observe

一次真实测试中，登录已成功，页面也已经进入业务主页，但 ReAct 连续输出 `observe_page`，最后触发重复动作保护。

原因：

- LLM 不确定下一步时倾向继续观察。
- prompt 没有明确要求“不要在同一页面重复 observe”。
- 页面正文中已经有 `您好，高斌！`，但 LLM 没有把它收敛成答案。

已做调整：

- prompt 增加：如果答案已在 `page_text` 或 element context 中出现，应返回 `finish` 并把简短答案放入 `action.value`。
- prompt 增加：不要在同一页面重复 `observe_page`，应选择具体 `click/type/press/extract_text/finish`。

仍需后续继续验证和增强最终答案提取。

## 9. 当前代码状态

### 9.1 已完成

- LLM ReAct 已进入 browser agent 主路径。
- 登录页控件由 LLM 基于 DOM observation 判断。
- 凭据仍由本地 `CredentialStore` 注入，不暴露给 LLM。
- `target_id` 执行链路修复。
- observation 增强页面正文和元素上下文。
- DOM ID 唯一性修复。
- 登录慢加载等待优化。
- 明确登录失败信号识别。
- `--max-steps` 生效修复。
- `.venv/` 加入 `.gitignore`。
- 全量测试通过。

### 9.2 当前保留但暂不作为主路径

workflow 相关能力仍保留：

- `search_user`
- `create_user`
- `assign_role`
- `create_user_and_assign_role`

但当前目标是先让 LLM ReAct 跑顺，所以自然语言 web 请求不再主动绑定这些 workflow。后续可以把 workflow 作为“可选专家工具”或“高置信业务 shortcut”，而不是默认主路径。

## 10. 后续建议

### 10.1 提升 ReAct 成功率

建议继续强化 LLM browser prompt 和 observation：

- 给 LLM 明确“当前任务是查用户登录名，不是查客户信息”的任务约束。
- 提供更清晰的动作历史摘要，例如“已经登录成功，当前页面包含：首页/客户信息管理/系统设置”。
- 当页面正文已经出现明显答案时，直接提示 LLM finish。
- 对连续相同页面 observe 做更智能的自动收敛。

### 10.2 增加最终答案抽取

当前 execution report 主要记录动作和最终 observation，但用户真正关心的是：

```text
高斌的登录名称是什么？
```

建议新增一个结果抽取阶段：

1. 浏览器任务结束或接近结束时，把最终 page_text、visible_messages、关键 elements 发给 LLM。
2. 让 LLM 输出结构化 answer。
3. `ResultSummarizer` 在 success 或 blocked-but-informative 状态下展示答案候选。

这样即使导航没有完全进入目标页面，也能从当前页面线索中给出“当前页面显示登录用户/姓名为高斌，但未找到登录名称字段”等明确反馈。

### 10.3 改善浏览器可观测性

建议新增：

- 每一步截图可选保存，而不是只有最终截图。
- 每一步动作后的 URL、页面标题、关键 page_text diff。
- 在 CLI 中输出当前 step、action、target，而不是只输出 LLM 调用日志。
- trace viewer 使用说明。

这样用户能明确看到“是否点击了登录”“点击了哪个元素”“页面是否跳转”。

### 10.4 Playwright 浏览器安装

本轮遇到 Playwright 自带 Chromium 下载卡住。临时方案是使用本机 Chrome：

```bash
--browser-channel chrome
```

后续建议：

- 在 README 或运行提示中说明需要 `playwright install chromium`。
- 若安装失败，可使用系统 Chrome/Edge channel。
- 检查 Playwright 下载进程残留和缓存锁问题。

## 11. 结论

本轮迭代完成了 Web Agent 从“固定 workflow 自动化”向“LLM ReAct 浏览器 Agent”的关键转向。

最重要的变化是：登录和后续网页动作不再主要依赖代码写死的字段和流程，而是让 LLM 基于 DOM observation 选择动作；执行层仍保持受控、安全、可审计。真实站点测试表明，agent 已经能够正确识别登录页 DOM、填写凭据并进入业务页面。

当前最大的剩余问题不是“能不能登录”，而是“登录后的复杂后台页面中，ReAct 如何更快、更准地找到目标功能并抽取最终答案”。这部分需要继续围绕 observation 质量、prompt 约束、动作去重、最终答案抽取和可观测性迭代。
