# 2026-06-17 会话总结：Web Skill 显式调用、凭据映射与 ifinance 查询链路修复

## 1. 背景

本次会话围绕 opsAgent 的 web_action / web skill 能力连续推进，核心目标是让 ifinance 这类多步骤网页任务能够：

- 通过自然语言或显式命令调用可复用 web skill。
- 根据 `credentials.local.json` 中的网站、用户、账号密码映射自动登录。
- 在浏览器确认门后正确恢复执行，不跨线程复用 live Playwright 对象。
- 将成功执行过的网页任务沉淀为参数化 skill。
- 对相同任务稳定输出足够完整的结果，而不是提前结束、漏点按钮或返回过于简单的信息。

会话中多次使用真实 `opsAgent chat` 跑 ifinance 任务验证，最后已经验证同一条查询 `pen_test2` 的任务可以成功执行并返回完整用户信息。

## 2. 原始问题链路

### 2.1 确认恢复与 Playwright 跨线程问题

最初定位到的根因链条是：

1. CLI 的 `stream_run` 会在后台线程启动浏览器。
2. 用户确认动作是在主线程继续。
3. live Playwright 对象被跨线程复用。
4. Playwright 对象线程绑定，跨线程复用会导致恢复链路不稳定。

修复方向不是让 Playwright 对象跨线程可用，而是让确认后的执行继续回到原浏览器上下文所属线程，或者在进程/上下文丢失时通过 checkpoint 和 storage state 重建浏览器上下文。

相关验证覆盖在 `tests/test_phase2_resume.py`：

- `test_controller_confirm_resumes_pending_browser_action`
- `test_controller_confirm_keeps_live_browser_on_original_thread`
- `test_controller_confirm_crash_resumes_web_subgraph_from_checkpoint`

### 2.2 ifinance 登录缺少 credential_ref

早期真实任务失败信息：

```text
异常信息：登录任务缺少 credential_ref 或凭据配置
```

用户希望以后可以直接用类似 `ifinance-check-admin`、`ifinance-init-admin` 这种账号标识登录对应网站，并进一步希望不从 ref 名称推断 site key，而是让 `credentials.local.json` 明确维护 `site_key` 与 `user` 的对应关系。

最终设计为：

- 兼容旧格式 `credentials`。
- 新增/支持站点结构 `sites.<site_key>.users.<user>`。
- 每个用户可以配置 `ref`、`username`、`password`。
- 站点可以配置 `default_user`。
- 运行时可通过 `site_key + user` 解析出 `credential_ref`。

主要实现位于 `src/aiops_agent/browser/credentials.py`。

### 2.3 ifinance 业务场景拆分

用户描述了更复杂的业务场景：

- 检查用户是否存在，不存在则创建用户。
- 检查用户岗位是否存在，不存在则分配岗位并启用。
- 分配用户账户，账户不在可分配列表时需要单位间授权。
- 创建、分配、授权等动作都需要登录、菜单导航、输入、确认、复核等多个网页步骤。

当时形成的设计判断：

- 本轮不直接实现完整业务编排 DSL。
- web skill 粒度应优先拆成“可复用的单个网页能力”，例如：
  - 查询用户。
  - 创建用户。
  - 用户复核。
  - 查询岗位。
  - 分配岗位。
  - 启用岗位。
  - 查询可分配账户。
  - 单位间授权。
  - 分配账户。
- 多 skill 的 if/else、补偿、复核账号切换等复杂业务流程，后续再由 workflow/DSL 做编排。

本次实现明确保持“单 skill 显式调用 / 单 skill 自动匹配”的边界。

## 3. Web Skill 显式调用方案

### 3.1 新增 chat 命令

在 `src/aiops_agent/chat.py` 中新增和完善了以下命令：

```text
/skills
/skill <skill-name> key=value ...
/skill <skill-name> --help
/delete-skill <skill-name> [--yes]
/remove-skill <skill-name> [--yes]
```

行为说明：

- `/skills`：列出可用 web skill，显示名称、站点、必填输入、运行时输入、描述。
- `/skill <skill-name> key=value ...`：显式执行某个 skill。
- `/skill <skill-name> --help`：显示该 skill 的 inputs、runtime inputs、site key。
- `/delete-skill`：删除已保存 skill，默认二次确认，`--yes` 可跳过确认。
- `/remove-skill`：作为 `/delete-skill` 的别名。

参数解析使用 `shlex.split`，支持带空格或中文的值，例如：

```text
/skill ifinance-search-user login_name=lvjing_1228 org_name="101-51011000 内部客户"
```

### 3.2 Controller 增加 skill 接口

在 `src/aiops_agent/agent/controller.py` 中新增：

- `list_web_skills()`
- `run_web_skill(...)`
- `delete_web_skill(...)`

这些接口让 chat 层不直接理解 skill 存储、渲染、凭据、站点配置等细节。

### 3.3 WebSkillInvocationService 抽离 glue code

为减少 controller 中的 web skill glue code，新增：

```text
src/aiops_agent/browser/skills/invocation.py
```

核心类：

- `WebSkillInvocation`
- `WebSkillInvocationService`

职责：

- 汇总 skill 显式调用所需的参数。
- 将 `site_key`、`user`、`credential_ref` 等特殊参数与 skill inputs 分离。
- 校验必填 inputs。
- 根据 `site_key + user` 或默认用户解析 `credential_ref`。
- 构造 `ExecutionPlan` 与 `ToolCallSpec`。
- 生成 browser_agent 所需参数，包括 `actions`、`skill_name`、`skill_parameters`、`site_config`、`requires_login`。

这样 controller 主要负责任务生命周期、事件、session 和 tool 调度，skill 调用准备逻辑下沉到专门服务。

## 4. Web Skill 命名与 schema

用户指出 skill 不适合使用点分命名，例如 `ifinance.user.search`。

本次沿用更接近目录名、命令名和文件系统友好的短横线命名：

```text
ifinance-search-user
ifinance-create-user
demo-search-user
```

相关规则位于 `src/aiops_agent/browser/skills/validator.py`：

- 仅允许小写字母、数字、短横线。
- 不能以短横线开头或结尾。
- 不能出现连续短横线。
- 最长 64 字符。

skill schema 继续兼容：

- `opsagent.web_skill.workflow.v1`
- `opsagent.web_skill.workflow.v2`

本轮未做大规模 schema 迁移。

## 5. Skill 匹配与显式执行

### 5.1 自动匹配

普通自然语言 `web_action` 仍然走自动匹配：

- 使用 `WebSkillMatcher.match(...)`。
- 根据关键词、字段、site key 等信息评分。
- 达到阈值时只选择一个最合适 skill。
- 命中后设置：
  - `auto_plan=False`
  - `actions`
  - `skill_name`
  - `skill_parameters`
  - `skill_execution`

匹配不到 skill 时回落到原有 planner。

### 5.2 显式执行

显式 `/skill` 不走关键词评分，而是按名称加载：

- `WebSkillMatcher.match_by_name(...)`
- 校验 skill 存在。
- 校验 workflow 格式。
- 校验 site key 是否冲突。
- 校验 required inputs。
- 渲染 actions。

如果 `BrowserAgentTool` / `WebAgentSubgraph` 收到 `skill_name` 但没有预构建 `actions`，会按名称加载 skill 并渲染动作。

### 5.3 fallback 策略

skill 执行失败时保留 fallback 机制：

- 登录失败、站点不可用、系统缺少信息等系统性失败不盲目 fallback。
- 普通执行失败可在配置允许时 fallback 到 LLM planner 一次。
- 遇到 awaiting confirmation 不 fallback，保持确认门。

相关逻辑位于 `src/aiops_agent/browser/subgraph.py`。

## 6. 凭据配置与 runtime 参数

### 6.1 新凭据模型

`BrowserCredential` 新增：

- `site_key`
- `user`

`CredentialStore` 新增能力：

- `site_key_for_ref(ref)`
- `default_user_for_site(site_key)`
- `ref_for_site_user(site_key, user)`
- `default_ref_for_site(site_key)`
- `ref_from_text(text)`

支持配置形态示意：

```json
{
  "sites": {
    "ifinance": {
      "default_user": "check-admin",
      "users": {
        "check-admin": {
          "ref": "ifinance-check-admin",
          "username": "...",
          "password": "..."
        },
        "init-admin": {
          "ref": "ifinance-init-admin",
          "username": "...",
          "password": "..."
        }
      }
    }
  }
}
```

### 6.2 `/skills` 中展示 runtime inputs

用户发现 `/skills` 只列 workflow inputs，没有告诉运行时还需要登录用户。

已调整为：

- skill 自身 inputs：来自 workflow。
- runtime inputs：由登录需求与凭据配置推导。
- 如果 skill 需要登录但没有可解析默认凭据，则 `/skills` 与 `/skill --help` 会提示需要 `site_key`、`user`。
- 如果存在默认 user，则展示为可选或带默认解析能力，不要求用户每次输入 `credential_ref`。

### 6.3 不推荐运行时直接输入 credential_ref

用户指出 `credential_ref` 不适合每次运行时输入。

最终策略：

- `/skill` 可继续兼容 `credential_ref`，但它只是特殊参数。
- 推荐使用 `site_key` 和 `user`。
- 如果 skill workflow 已有 `site_key` 且 `credentials.local.json` 有默认用户，则无需额外输入。
- 内部再解析成 `credential_ref` 传给 browser_agent。

## 7. `/save-skill` 参数化增强

用户发现 `/save-skill` 沉淀 skill 时，该参数化的内容没有参数化。

本次增强 `src/aiops_agent/browser/skills/generator.py`：

- 从成功任务的 canonical action trace 或 legacy steps 中提取动作。
- 跳过 secret 动作，例如用户名、密码、token、cookie、storage state。
- 对 `type`、`select`、`press` 中的输入值做参数化判断。
- 将任务目标中的显式输入字段转成 workflow inputs。
- 支持把固定值与变量值分开记录。
- 对动态结果类动作清理 value，例如 `finish`、`extract_text`、`save_artifact` 的结果文本不沉淀为固定值。
- 生成 `parameterization_decisions`，供 `/save-skill` 输出预览。
- 对需要返回答案但成功结果里没有明确 answer 的任务，拒绝沉淀为 skill。

chat 中 `/save-skill` 输出增加：

- 参数列表。
- 动作数。
- 匹配关键词。
- 参数预览。
- 固定值预览。

## 8. 删除 skill 命令

新增删除命令：

```text
/delete-skill <skill-name>
/delete-skill <skill-name> --yes
/remove-skill <skill-name>
```

默认行为：

- 需要输入 `yes` 或 `y` 二次确认。
- `--help` 显示用法。
- 未知选项给出错误。
- 删除失败时输出明确原因。

底层由 `WebSkillStore.delete(name)` 完成目录删除和校验。

## 9. ifinance 用户查询任务的问题定位与修复

用户提供的真实任务：

```text
查询用户pen_test2的信息：用ifinance-check-admin登录ifinance,
在左侧侧边栏依次点击网上银行管理，用户信息管理，网银用户管理进入对应菜单,
等待页面加载完成，然后在加载出的iframe中找到查询按钮，点击查询,
在弹出的内容中找到用户名称,
在用户名称中输入pen_test2,
然后点击确定按钮,
告诉我用户对应的信息
```

### 9.1 输入框定位问题

一开始 agent 找不到或误找“用户名称”的输入框。

定位到页面中存在：

- 查询弹窗里的 `userName` 文本输入框。
- 表格行里的 checkbox/radio。
- 全局菜单搜索框 `searchInput`。

修复点在 `src/aiops_agent/browser/playwright_tool.py`：

- 新增 `_editable_input_css_selector`。
- 新增 `_editable_input_xpath`。
- label 查找时排除：
  - hidden
  - checkbox
  - radio
  - button
  - submit
  - reset
  - image
  - file
  - password，除非明确允许。
- 对“用户名称”等常见字段优先用语义 selector，例如 `userName`。

新增测试：

- `test_playwright_tool_type_label_ignores_table_row_checkboxes`

### 9.2 输入后没有点击最终确定

用户重新运行后，输入框已经正确，但流程在没有点击最后“确定”时结束。

通过 execution report 看到动作序列：

```text
click 查询
observe
type userName pen_test2
extract_text
finish
```

缺少 `click 确定`。

修复点在 `src/aiops_agent/browser/agent.py`：

- `_stabilize_action(...)` 增加 pending explicit input 检查。
- 如果用户目标中还有未完成的显式输入，而 planner 给出 `click/extract_text/finish/save_artifact`，先补 `type`。
- 如果已经完成显式输入，但用户目标里紧跟着“然后点击 X”，而 planner 给出 `extract_text/finish/save_artifact`，先补 `click X`。
- 对输入值重复出现的任务，使用显式输入语句的 match end 定位后续点击，而不是用 `find(value)` 找第一个出现位置。

新增测试：

- `test_browser_agent_stabilizes_click_to_pending_explicit_input`
- `test_browser_agent_stabilizes_extract_to_expected_click_after_type`
- `test_browser_agent_uses_nearest_click_after_repeated_typed_value`

### 9.3 `确定` 与英文 `Search` 的别名问题

真实 ifinance 页面中，弹窗的“确定”按钮显示为英文 `Search`。

第一次修复后，agent 能规划 `click Search`，但曾误选中左侧全局菜单输入框：

```text
searchInput
```

根因：

- `确定` 的英文别名包含 `Search`。
- 原 `_find_element` 不区分普通输入框和可点击按钮。
- 字符串包含匹配会让 `searchInput` 命中 `Search`。

修复：

- `_find_element(...)` 只在真正可点击元素里找：
  - `a`
  - `button`
  - `link`
  - `menuitem`
  - `input[type=button|submit|reset|image]`
- 英文别名匹配使用词边界，避免 `searchInput` 这种拼接词误命中。
- `_click_label_aliases("确定")` 保留 `Search`，用于英文 UI。

新增测试：

- `test_browser_agent_expected_click_prefers_clickable_search_alias`

### 9.4 意图校验误拦截 `Search`

后续真实运行时出现新的 blocked：

```text
规划动作与用户意图不一致，已停止执行：
用户要求下一步点击 确定，但当前动作目标是 Search。
```

说明执行器已经知道 `Search` 是 `确定`，但 `_action_intent_alignment(...)` 仍用中文直连判断。

修复：

- intent alignment 也改用 `_click_target_matches_label(...)`。
- 输入字段校验也改用字段别名，避免 `用户名称` 与 `userName` 被误判不一致。

### 9.5 查询结果提取过简或 blocked

真实页面点击后已经有结果行，但任务仍 blocked：

```text
任务要求返回答案，但未能从当前页面提取到明确结果。
```

page_text 中的结果表是英文表头：

```text
User List
User No
User Name
Login Name
Subordinate Units
Duty Name
Input User Name
Modify Name
Check User Name
Check Time
Status
Check State
U0003085 pen_test2 pen_test2 101-51011000_内部客户 ...
Activity in Has been reviewed
```

原有提取逻辑偏向中文表头或窄字段回答，无法泛化成“告诉我用户对应的信息”这种宽泛详情回答。

新增 `src/aiops_agent/browser/table_extractor.py`：

- `TextTableExtractor.detail_answer(...)`
- `TextTableExtractor.column_matches(...)`
- 支持宽泛输出字段：
  - 信息
  - 详情
  - 资料
  - 明细
  - 记录
  - 结果
- 支持中英文表头别名：
  - `User No` -> `用户编号`
  - `User Name` -> `用户名称`
  - `Login Name` -> `登录名称`
  - `Subordinate Units` -> `所属单位`
  - `Input User Name` -> `录入人`
  - `Check User Name` -> `复核人`
  - `Check Time` -> `复核日期`
  - `Status` -> `活动状态`
  - `Check State` -> `复核状态`
- 支持英文多词表头合并。
- 支持少量空列对齐，例如岗位列为空。
- 支持英文状态值翻译：
  - `Activity in` -> `活动中`
  - `Has been reviewed` -> `已复核`
- 对详情回答只保留接近最高分的候选，避免结果膨胀。
- 对列查询保留页面顺序，避免影响模糊匹配与精确匹配并存的场景。

新增测试：

- `test_browser_agent_extracts_full_user_info_for_broad_info_request`
- `test_browser_agent_extracts_user_info_from_english_table_headers`
- `test_browser_agent_extracts_generic_table_detail_for_broad_info_request`
- `test_browser_agent_extracts_generic_table_column_answer`

## 10. 真实 ifinance 验证结果

最后使用真实 `opsAgent chat` 重新运行同一任务。

启动命令：

```bash
.venv/bin/aiops-agent chat \
  --config configs/rpa.json \
  --llm-config configs/llm.json \
  --credential-config configs/credentials.local.json \
  --headless \
  --max-steps 24 \
  --browser-slow-mo 100
```

确认门出现：

```text
待执行动作: click -> Search
预期结果: Click the 确定 button to submit the query for pen_test2 and display the user's information.
确认继续执行? [y/N]
```

输入 `y` 后，任务继续执行并成功返回：

```text
pen_test2的信息：用户编号U0003085，用户名称pen_test2，登录名称pen_test2，所属单位101-51011000_内部客户，录入人U0000003，录入日期2026-06-12，复核人U0000004，复核日期2026-06-12，活动状态活动中，复核状态已复核。
```

最终状态：

```text
执行状态: success
```

## 11. 测试结果

本次最后一轮验证通过：

```bash
.venv/bin/python -m compileall \
  src/aiops_agent/browser/agent.py \
  src/aiops_agent/browser/table_extractor.py \
  tests/test_browser_workflow.py
```

结果：通过。

```bash
.venv/bin/pytest tests/test_browser_workflow.py -q
```

结果：

```text
32 passed
```

```bash
.venv/bin/pytest -q
```

结果：

```text
245 passed, 10 skipped, 1 warning
```

唯一 warning 来自第三方依赖 `trustcall` 使用 LangGraph deprecated import：

```text
LangGraphDeprecatedSinceV10: Importing Send from langgraph.constants is deprecated.
```

本次没有处理该 warning，因为它不是当前功能回归。

## 12. 主要改动文件

### 12.1 Chat 与 Controller

- `src/aiops_agent/chat.py`
  - 新增 `/skills`。
  - 新增 `/skill`。
  - 新增 `/delete-skill`、`/remove-skill`。
  - confirmation 交互支持循环恢复。
  - 优先使用 controller.run，减少 chat 场景下 stream_run 后台线程带来的 live browser 线程问题。

- `src/aiops_agent/agent/controller.py`
  - 新增 web skill 列表、执行、删除接口。
  - 补充 credential_ref / site_key / user 解析链路。
  - 支持确认恢复 web_action。
  - 支持 LangGraph interrupt/resume。
  - 支持 browser subgraph state 查询与恢复。

### 12.2 Web Skill

- `src/aiops_agent/browser/skills/invocation.py`
  - 新增显式 skill 调用服务。

- `src/aiops_agent/browser/skills/matcher.py`
  - 新增按名称匹配能力。
  - 保持普通自然语言单 skill best-match。

- `src/aiops_agent/browser/skills/generator.py`
  - 增强 `/save-skill` 参数化。
  - 过滤动态结果和敏感字段。
  - 输出参数化决策。

- `src/aiops_agent/browser/skills/store.py`
  - 支持删除 skill。

- `src/aiops_agent/browser/skills/validator.py`
  - 使用短横线 skill 命名规则。

### 12.3 Browser 执行链路

- `src/aiops_agent/browser/subgraph.py`
  - 显式 skill_name 但没有 actions 时，按名称加载并渲染。
  - 自动匹配命中后设置 actions 与 skill_execution。
  - 保留确认门与 fallback 机制。

- `src/aiops_agent/browser/agent.py`
  - pending explicit input 稳定化。
  - pending expected click 稳定化。
  - click 目标中英文别名。
  - 只选择真正可点击元素作为 click 后续目标。
  - intent alignment 使用同一套别名规则。
  - 接入 `TextTableExtractor`。

- `src/aiops_agent/browser/playwright_tool.py`
  - 输入框定位只选择可编辑输入控件。
  - 排除 checkbox/radio/button 等非文本输入。
  - 增强 iframe 内字段定位。

### 12.4 Credentials

- `src/aiops_agent/browser/credentials.py`
  - 支持 `site_key`。
  - 支持 `user`。
  - 支持 `sites.<site_key>.users.<user>`。
  - 支持默认用户和站点默认凭据解析。

### 12.5 结果提取

- `src/aiops_agent/browser/table_extractor.py`
  - 新增通用文本表格提取器。
  - 支持宽泛详情回答和列字段回答。
  - 支持中英文表头别名和状态值翻译。

## 13. 测试覆盖新增重点

### 13.1 Chat 命令

- `/skills` 列表展示。
- `/skill --help` 展示 inputs / runtime inputs。
- `/skill key=value` 参数解析。
- quoted 参数与中文值解析。
- 参数缺失错误。
- 删除 skill 的确认、取消、`--yes`。

### 13.2 Credentials

- 旧 credentials 格式兼容。
- 新 `sites` 格式加载。
- `site_key_for_ref`。
- `default_user_for_site`。
- `ref_for_site_user`。
- `default_ref_for_site`。

### 13.3 Web Skills

- 自动匹配单 skill。
- site key 不匹配时不命中。
- 按名称执行不依赖关键词。
- required input 缺失时报错且不启动浏览器。
- runtime user 默认解析。
- `site_key + user` 解析 `credential_ref`。
- `/save-skill` 参数化与动态结果过滤。

### 13.4 Browser Workflow

- 显式 skill_name 无 actions 时按名称加载。
- 确认恢复保持原线程 live browser。
- crash resume 可从 checkpoint/storage state 重建。
- 输入字段定位避免 checkbox 干扰。
- 输入后自动补后续点击。
- `确定` 与 `Search` 的按钮别名。
- `searchInput` 不再被误当按钮。
- 英文表头结果提取。

## 14. 当前边界与后续建议

### 14.1 业务编排 DSL 尚未实现

本次没有实现复杂 ifinance 业务编排 DSL。

当前能力适合：

- 单个 skill 显式调用。
- 普通自然语言命中一个最合适 skill。
- skill 失败后有限 fallback。

后续如果要覆盖“检查用户 -> 不存在则创建 -> 切换复核账号 -> 分配岗位 -> 授权账户”等流程，建议引入业务编排层：

- 明确 step 输入输出。
- 支持条件分支。
- 支持账号切换。
- 支持复核步骤。
- 支持失败补偿和人工介入点。
- 支持每个 web skill 的结构化 result contract。

### 14.2 表格提取器需要持续扩展别名

`TextTableExtractor` 目前覆盖了本次 ifinance 用户查询与一批通用字段。

后续遇到更多系统表格时，可以继续扩展：

- 英文字段别名。
- 状态值翻译。
- 分页控件 stop token。
- 更复杂的多行表头。
- 单元格为空时的对齐评分规则。

### 14.3 Controller 仍有继续瘦身空间

虽然本次已经抽出了 `WebSkillInvocationService`，但 controller 仍承担：

- 主图 orchestration。
- confirmation resume。
- web skill 自动匹配事件。
- browser tool 结果汇总。
- memory/session/task 维护。

后续可以进一步拆：

- `ConfirmationCoordinator`
- `BrowserTaskRunner`
- `WebSkillOrchestrator`
- `TaskLifecycleService`
- `ProgressEventEmitter`

### 14.4 skill schema 暂未迁移

本次继续复用 `workflow.json` 的：

- `inputs`
- `actions/steps`
- `site_key`
- `execution`

后续可以考虑 v2 schema 明确增加：

- runtime inputs。
- result contract。
- mutation metadata。
- reviewer account requirements。
- cross-skill dependencies。

## 15. 最终结论

本次会话完成了三条主线：

1. web skill 从“只能自动隐式命中”推进到“可列出、可 help、可显式执行、可删除、可参数化保存”。
2. ifinance 登录从“缺 credential_ref”推进到“基于 credentials.local.json 的 site_key/user/default_user 自动解析”。
3. 真实 ifinance 查询链路从“找不到输入框 / 漏点确定 / Search 被误判 / 提取结果 blocked”推进到“确认后成功点击并返回完整用户信息”。

最后真实验证的成功输出为：

```text
pen_test2的信息：用户编号U0003085，用户名称pen_test2，登录名称pen_test2，所属单位101-51011000_内部客户，录入人U0000003，录入日期2026-06-12，复核人U0000004，复核日期2026-06-12，活动状态活动中，复核状态已复核。
```

