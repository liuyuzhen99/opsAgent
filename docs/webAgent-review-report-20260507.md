# Web Agent MVP Review Report - 2026-05-07

## 1. 背景与目标

今天的目标是把 opsAgent 中已有的浏览器自动化雏形推进到一个可运行、可确认、可演示的 Web Agent MVP。

这次实现没有把 opsAgent 改造成通用 browser-use 替代品，而是把 Web Agent 定位为 opsAgent 的一个受控能力模块，优先服务固定后台系统中的账号和权限操作：

1. 用户用自然语言描述任务，例如“创建账号 carol，邮箱 carol@example.com，分配只读权限”。
2. opsAgent 识别为 `web_action`。
3. 系统按站点配置生成固定业务 workflow。
4. Playwright 打开浏览器页面并执行安全本地动作。
5. 遇到保存、提交、授权等远端写操作时进入人工确认。
6. 用户确认后，系统回放安全动作并执行已确认的写操作。
7. 最终保存截图、页面摘要、执行报告、session state 和审计事件。

这条路线借鉴了 browser-use 的核心思路：浏览器会话、页面 observation、结构化动作、受限执行、循环修复和 artifact 输出。但第一版没有引入完整 ReAct 自主规划，而是选择“站点配置 + 固定 workflow + 受控动作 + 人工确认”的实现方式。

## 2. 做了什么

### 2.1 新增站点配置能力

新增了 `browser_sites.json` 配置入口，用 `site_key` 描述一个固定后台系统。

配置中包含：

- `base_url`
- `login_url`
- `allowed_domains`
- `login_fields`
- `workflows`
- 每个 workflow 的入口 URL、按钮文本、字段定位提示和成功信号

当前支持三个 workflow：

- `create_user`
- `assign_role`
- `create_user_and_assign_role`

同时新增 Pydantic v2 校验模型，用来校验站点配置是否缺少必要 URL、workflow 或字段结构。这样配置错误会在任务开始前暴露，而不是运行到浏览器中间才失败。

### 2.2 扩展自然语言意图解析

扩展了 `IntentParser`，让账号和权限类请求不再依赖“网页”“浏览器”这类显式关键词。

现在可以识别：

- 创建账号
- 新建账号
- 创建用户
- 新建用户
- 分配角色
- 分配权限
- 授权
- 只读权限

并抽取固定字段：

- `username`
- `email`
- `display_name`
- `department`
- `role`

例如：

```text
创建账号 carol，邮箱 carol@example.com，分配只读权限
```

会被解析为：

```json
{
  "intent": "web_action",
  "workflow": "create_user_and_assign_role",
  "workflow_fields": {
    "username": "carol",
    "email": "carol@example.com",
    "role": "只读权限"
  }
}
```

### 2.3 CLI 增加 Web Agent 参数

新增和扩展了 CLI 参数：

```bash
--browser-site <site_key>
--browser-sites-config <path>
--credential-config <path>
--credential-ref <ref>
--allowed-domains <domains>
--headed
--browser-trace
--browser-video
--browser-channel chromium|msedge|chrome
--browser-slow-mo <ms>
```

其中今天新增或强化的重点是：

- `--browser-site`：强制按指定站点进入 Web Agent workflow。
- `--browser-sites-config`：加载固定后台系统配置。
- `--browser-channel`：允许 Playwright 启动 Edge 或 Chrome。
- `--browser-slow-mo`：让浏览器动作变慢，便于可视化观察。

### 2.4 Browser Planner 改为 workflow 驱动

原先 planner 更偏通用规则：打开 URL、观察页面、简单输入、保存 artifact。

今天新增了固定业务 workflow：

```text
打开站点入口
观察页面
进入用户管理页
打开新建用户表单
填写用户名
填写邮箱等字段
保存用户前阻断
确认后保存用户
进入权限页
选择角色
保存权限前阻断
确认后保存权限
保存 artifact
finish
```

关键设计点：

- workflow 由站点配置决定，不让 LLM 自由生成任意操作。
- LLM schema 先作为边界模型加入，但 MVP 主路径仍是规则和配置驱动。
- 每个 workflow action 带 `key`，例如 `create_user.field.username`、`create_user.submit`、`assign_role.submit`，用于确认恢复和跳过已完成的提交动作。

### 2.5 修复 observation 的可执行元素 ID

之前 observation 会生成类似 `el-0` 的元素 ID，但执行时 Playwright 查找的是页面上的 `data-aiops-id`。如果页面原本没有这个属性，agent 看到的 ID 无法执行。

今天修复为：

- observation 时给可交互 DOM 节点注入稳定 `data-aiops-id`。
- 返回的 `element_id` 与真实 DOM attribute 对齐。
- 后续 LLM 或 workflow 选择 `target_id` 时可以真实执行。

这是让 Web Agent 从“能观察”走向“能可靠执行”的关键修复。

### 2.6 修复人工确认后的恢复逻辑

原先 `confirm` 的逻辑主要是恢复 URL 和 storage state，然后直接执行 pending action。这在真实表单场景会丢失未提交的本地表单值。

今天改为：

1. 第一次运行时，遇到远端写操作前阻断。
2. 阻断结果里保存：
   - `pending_action_raw`
   - `replay_actions`
   - `completed_action_keys`
   - `resume_url`
   - `session_state_path`
3. 用户执行 `confirm <task_id>` 后：
   - 先回放安全本地动作，例如打开页面、点击新建、填写字段、选择角色。
   - 再执行已确认的 unsafe mutation。
   - 对已经确认完成的提交动作，通过 `completed_action_keys` 跳过，继续后续 workflow。

这解决了“确认后表单值丢失”和“创建用户后继续分配权限”两个实际问题。

### 2.7 增加 LLM Planner 输出 schema

新增 `BrowserPlannerOutput`，限制 LLM 只能输出固定动作：

- `open_url`
- `click`
- `type`
- `select`
- `wait_for`
- `observe_page`
- `save_artifact`
- `finish`

schema 会拒绝：

- 未知 action，例如 `eval_js`
- 非法 URL
- 缺少 target 的 click/type/select
- 缺少 value 的 type/select
- 额外字段

当前 MVP 还没有把 LLM planner 接入主路径，这是有意选择。先把 schema 边界固定下来，后续再把 LLM 用作定位失败修复、字段别名匹配和页面状态判断。

### 2.8 可见浏览器演示能力

为了满足“能在浏览器中看到操作动作”的要求，今天新增了：

- `--browser-channel msedge`
- `--browser-channel chrome`
- `--headed`
- `--browser-slow-mo`

验证命令示例：

```bash
aiops-agent run \
  --config /private/tmp/opsagent-web-demo/config/rpa.json \
  --llm-config /private/tmp/opsagent-web-demo/config/llm.json \
  --browser-sites-config /private/tmp/opsagent-web-demo/config/browser_sites.json \
  --browser-site mock-admin \
  --headed \
  --browser-channel msedge \
  --browser-slow-mo 900 \
  --max-steps 12 \
  "创建账号 carol，邮箱 carol@example.com，分配只读权限"
```

这会启动一个新的可见 Edge 实例，由 Playwright 控制。用户可以直接看到页面打开、点击、填写、选择等动作。

## 3. 为什么这样做

### 3.1 不做通用 Web Agent

通用 Web Agent 的自由度太高，第一版很难同时保证：

- 不误点高危按钮
- 不跨域
- 不泄漏凭据
- 可恢复
- 可审计
- 可测试

opsAgent 当前的核心目标是运维场景下的账号和权限操作，所以更适合先做固定后台系统适配。

### 3.2 选择 workflow 驱动，而不是完整 ReAct 驱动

完整 ReAct 更适合探索未知网页，但账号/权限后台是强业务流程，核心要求是稳定、可控、可审计。

因此 MVP 选择：

- 固定 workflow 决定业务阶段。
- 站点配置决定入口、字段和按钮。
- Playwright 负责真实执行。
- RiskEvaluator 负责确认门禁。
- LLM 暂时只作为后续辅助修复能力。

这个选择牺牲了一部分泛化能力，但换来更高的安全性和可预测性。

### 3.3 选择提交前人工确认

创建用户、保存权限、授权、撤权都属于远端写操作。即使 mock 页面中只是本地 HTML，真实接入后台时这些动作会产生业务影响。

因此所有 `unsafe_mutation` 都进入确认门：

- agent 可以自动登录、打开页面、填写表单、选择角色。
- agent 不会自动点击保存、提交、授权。
- 用户确认后才执行对应 pending action。

这是当前阶段最重要的安全边界。

### 3.4 选择回放安全动作，而不是只恢复 URL

只恢复 URL 不足以恢复浏览器表单状态，因为未提交的 input/select 通常不在 cookie 或 storage state 中。

所以确认恢复必须回放：

- 打开页面
- 打开表单
- 填写字段
- 选择角色

然后再点击已确认的提交按钮。

这个方案比保存 DOM 快照更简单，比依赖浏览器 session 更可靠。

### 3.5 选择新 Edge/Chrome 实例，而不是接管当前浏览器

Playwright 可以启动 Edge/Chrome，也可以通过 CDP 连接已有浏览器。但接管用户当前普通浏览器窗口通常不可行，因为默认没有开启 remote debugging port。

今天选择：

- 用 Playwright 启动新的 Edge/Chrome 实例。
- 通过 `--headed` 和 `--browser-slow-mo` 实现可视化演示。

这是稳定性和可观察性之间的更好平衡。

## 4. 怎么做的

### 4.1 主要代码改动

核心改动集中在以下模块：

- `src/aiops_agent/browser/site_config.py`
  - 新增站点配置 Pydantic 模型和加载函数。

- `src/aiops_agent/browser/llm_planner.py`
  - 新增 LLM planner 输出 schema。

- `src/aiops_agent/browser/models.py`
  - 扩展 `BrowserTaskSpec` 和 `BrowserAction`，支持 workflow、site config、action key、replay actions、browser channel、slow mo。

- `src/aiops_agent/browser/planner.py`
  - 新增 workflow action 生成逻辑。

- `src/aiops_agent/browser/agent.py`
  - 新增 spec 校验。
  - 新增确认恢复回放。
  - 传递 browser channel 和 slow mo。

- `src/aiops_agent/browser/playwright_tool.py`
  - 支持 `channel` 和 `slow_mo`。
  - observation 时注入 `data-aiops-id`。

- `src/aiops_agent/agent/parser.py`
  - 扩展账号/权限类意图和字段抽取。

- `src/aiops_agent/agent/controller.py`
  - 注入 browser site 配置。
  - confirm 时传递 replay actions 和 completed action keys。

- `src/aiops_agent/planning.py`
  - 将 workflow、site config、browser 参数写入 browser tool call。

- `src/aiops_agent/cli.py`
  - 新增 Web Agent CLI 参数。

- `configs/browser_sites.json`
  - 新增默认空站点配置文件。

### 4.2 测试改动

新增或扩展测试：

- `tests/test_browser_site_config.py`
  - 配置加载和校验。
  - LLM action schema 校验。

- `tests/test_browser_workflow.py`
  - workflow 在远端写操作前阻断。
  - confirm 后回放安全动作。
  - 创建用户后继续进入分配权限。
  - 缺失关键字段时 blocked。

- `tests/test_intent_parser.py`
  - 账号/权限自然语言请求路由到 `web_action`。

另外补了 `tests/__init__.py`，让已有测试中 `from tests...` 的导入可以正常收集。

### 4.3 演示环境

使用临时 mock 后台：

```text
/private/tmp/opsagent-web-demo/site/users.html
/private/tmp/opsagent-web-demo/site/users/<username>/roles.html
```

HTTP 服务：

```bash
python3 -m http.server 8765 --bind 127.0.0.1
```

mock 站点配置：

```text
/private/tmp/opsagent-web-demo/config/browser_sites.json
```

演示任务：

```text
创建账号 carol，邮箱 carol@example.com，分配只读权限
```

最终验证结果：

- 第一次运行：停在 `create_user.submit` 前。
- 第一次确认：执行保存用户，进入权限页，选择只读权限，停在 `assign_role.submit` 前。
- 第二次确认：执行保存权限，任务成功。

最终 artifact：

```text
storage/artifacts/7af5f64b-cd7e-4fd3-b2c0-27fd0c397daa/963f0e1f-c522-45ba-a85b-cf87180d4cc8/
```

页面摘要显示：

```text
title=Mock Admin - Roles
url=http://127.0.0.1:8765/users/carol/roles.html
page_type=interactive
messages=carol 权限 | 权限分配成功
elements=角色 | 保存权限
```

## 5. 验证结果

### 5.1 自动化测试

执行：

```bash
pytest -q
```

结果：

```text
31 passed
```

覆盖内容包括：

- 站点配置校验。
- LLM planner action schema。
- 自然语言账号/权限意图解析。
- workflow 阻断。
- confirm 回放。
- 角色分配继续执行。
- 缺少字段 blocked。
- 既有浏览器登录、风险、planner、resume 测试。

### 5.2 手工可视化验证

使用可见 Edge 实例验证：

```bash
aiops-agent run \
  --browser-site mock-admin \
  --headed \
  --browser-channel msedge \
  --browser-slow-mo 900 \
  "创建账号 carol，邮箱 carol@example.com，分配只读权限"
```

实际观察到：

- 新 Edge 实例启动。
- 自动打开用户管理页。
- 自动点击“新建用户”。
- 自动填写用户名和邮箱。
- 在“保存用户”前停止。
- confirm 后自动回放并点击“保存用户”。
- 自动进入 `carol` 权限页。
- 自动选择“只读权限”。
- 在“保存权限”前停止。
- 第二次 confirm 后点击“保存权限”并成功完成。

## 6. 当前能力边界

当前已经具备：

- 固定站点配置。
- 自然语言触发账号/权限 workflow。
- 创建用户表单填写。
- 角色/权限选择。
- 提交前人工确认。
- 确认后回放安全动作。
- 多阶段确认。
- Playwright Chromium/Edge/Chrome 可见执行。
- 截图、页面摘要、执行报告、session state。
- LLM 输出 schema 边界。

当前还不具备：

- 通用网页探索。
- 复杂后台系统的自适应导航。
- 真正接入 LLM planner 做失败修复。
- 企业级凭据后端。
- 自动识别真实业务成功/失败的强校验。
- 多租户安全隔离。
- 更完整的 CDP 接管已有浏览器能力。

## 7. 后续需要做什么

### 7.1 P0：修复和加固当前 MVP

1. 将 mock demo 固化为集成测试 fixture，而不是依赖 `/private/tmp` 临时文件。
2. 将 `browser_channel` 和 `browser_slow_mo_ms` 加入 confirm 恢复参数，保证 confirm 也能继续使用可见 Edge 和慢速动作。
3. 优化报告文案，避免 Web Agent 成功后仍出现“巡检通过”这种 inspection 语义。
4. 清理或忽略 `__pycache__`、`*.pyc`、`storage/artifacts` 等运行产物，避免污染 git 状态。
5. 对 `storage_state`、截图和页面摘要做敏感文件治理，至少增加清理策略和路径说明。

### 7.2 P1：接入真实后台系统

1. 为第一个真实后台系统编写 `browser_sites.json` 配置。
2. 梳理真实字段：
   - 用户名
   - 邮箱
   - 显示名
   - 部门
   - 角色
   - 权限集
3. 增加真实登录流程配置。
4. 验证错误密码、MFA、验证码、角色不存在、重复账号、提交失败等分支。
5. 增加成功信号校验，不能只看按钮点击成功。

### 7.3 P2：LLM 辅助修复

在固定 workflow 不变的前提下接入 LLM：

- locator 失效时，根据 observation 选择等价元素。
- 页面文案变化时，匹配字段别名。
- 提交后根据页面消息判断成功或失败。
- 重复失败时生成 blocked reason。

LLM 只能输出 `BrowserPlannerOutput`，不能输出任意代码。

### 7.4 P3：企业级能力

1. 凭据后端替换为钥匙串、Vault 或云 KMS。
2. artifact 加密或按任务生命周期清理。
3. 审计事件增加操作者、审批人、审批时间和变更摘要。
4. 支持 dry-run 和审批流集成。
5. 支持 CDP 接入专用浏览器实例，用于更长生命周期的人工接管场景。

## 8. 结论

今天的实现把 Web Agent 从“浏览器自动化雏形”推进到了“固定后台账号/权限 workflow MVP”。

最关键的变化不是多了几个 Playwright 动作，而是补齐了真实可用所需的四个基础能力：

1. 站点配置驱动业务流程。
2. 可执行的页面 observation。
3. 远端写操作前人工确认。
4. 确认后的安全动作回放。

当前方案仍然保守，但这是有意选择。账号和权限操作属于高风险运维动作，第一版应优先做到可控、可恢复、可审计、可演示，再逐步引入 LLM 自适应修复和更通用的网页能力。
