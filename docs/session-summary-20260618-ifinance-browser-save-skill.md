# 2026-06-18 会话总结：ifinance 岗位查询稳定化、Web Skill 参数化与改名

## 1. 会话目标

本次会话围绕一条真实 ifinance 网页任务持续排查和实现，目标包括：

- 使用 `ifinance-check-admin` 登录 ifinance。
- 依次进入“网上银行管理 -> 权限管理 -> 网银岗位分配”。
- 在“授权单位”Select2 下拉框中搜索并选择 `101-51013200_内部客户`。
- 打开查询条件，在“用户名”字段输入 `U0002865`。
- 查询并选择第一条结果，打开“分配岗位 -> 已分配岗位”。
- 返回当前已分配岗位名称。
- 将成功任务保存为可复用 Web Skill。
- 确保 Skill 同时参数化授权单位和用户名，而不是只提取一个参数。
- 让保存的 Skill 能被自然语言自动匹配，也能显式执行。
- 增加 Skill 改名能力。

最终业务查询得到的岗位名称为：

```text
锦乔生物科技有限公司经办人
```

## 2. 原始任务指令

本次用于真实验证的完整指令为：

```text
使用ifinance-check-admin登录ifinance，在左侧侧边栏依次点击网上银行管理，权限管理，网银岗位分配进入对应菜单，等待页面加载完成，然后在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，之后点击下方高亮的第一个内容，之后点击查询按钮，在用户名中输入U0002865，然后点击下方的查询按钮进行查询，之后选中查询后的第一条数据，点击分配岗位，再点击弹出内容中的已分配岗位，告诉我当前已分配岗位中的岗位名称
```

早期现象包括：

- Agent 无法稳定点击 Select2 下拉框中的蓝色高亮候选项。
- `U0002865` 被输入到“登录名称”，而不是“用户名”。
- 授权单位值又被错误输入到“登录名称”。
- 页面上存在多个“查询”按钮，Agent 会点击错误按钮。
- 查询前后动作顺序不稳定，可能先选择表格行再查询。
- 保存 Skill 时只生成一个 username 参数，导致授权单位和用户名共用同一参数。

结论是：用户指令已经足够明确，主要问题在浏览器动作稳定化、字段语义识别和 Skill 参数提取，不应继续依赖用户反复修改措辞规避代码缺陷。

## 3. 浏览器任务稳定化

### 3.1 Select2 授权单位下拉框

针对 ifinance 使用的 Select2 组件，增加了专门的稳定化处理：

- 展开“授权单位”下拉列表后，优先定位当前激活的 Select2 搜索输入框。
- 不再把授权单位值输入普通页面文本框。
- 识别 `搜索中`、`Searching`、`results are available` 等中英文 Select2 状态文本。
- 搜索完成后点击当前高亮的第一条候选项。
- 支持候选项上方存在遮罩层时，优先点击弹出层中的匹配选项。

主要实现文件：

- `src/aiops_agent/browser/agent.py`
- `src/aiops_agent/browser/playwright_tool.py`

### 3.2 授权单位与用户名字段分离

从原始任务中分别提取：

```text
授权单位 = 101-51013200_内部客户
用户名   = U0002865
```

浏览器稳定化逻辑会按原始指令顺序执行，并将 `U0002865` 明确写入 `userName`，不再误写到 `loginName`。

对应行为包括：

- 授权单位输入必须先完成。
- 授权单位候选项必须先选中。
- 第一个“查询”用于打开查询条件。
- `U0002865` 必须输入“用户名”。
- 第二个“查询”用于提交查询条件。

### 3.3 多个“查询”按钮的消歧

页面同时存在工具栏查询、查询条件弹窗查询等多个同名按钮。

本次增加：

- `_find_command_element`
- `_find_query_button`
- `_query_button_score`

查询条件区域的按钮优先于工具栏或包含其他命令上下文的 Query 元素。

Playwright locator 解析也调整为优先使用 `target_id`，再使用通用命令别名，避免日志记录了正确元素 ID，但实际点击页面第一个同名按钮。

### 3.4 查询与表格行选择顺序

修复内容：

- 当用户要求“查询后的第一条数据”时，将模型生成的用户编号点击规范化为“第一条数据”。
- 如果仍有待执行的查询提交动作，不允许提前选择表格行。
- jqGrid 行选择只选择第一条业务数据，不选择表头。
- 支持遮罩层或点击拦截情况下的表格行点击。

### 3.5 已分配岗位结果提取

结果识别支持中文和英文页面标记，包括：

- `已分配岗位`
- `Assigned position`
- `Assign duty Name`

最终可从页面提取：

```text
锦乔生物科技有限公司经办人
```

### 3.6 登录动作风险分类

登录动作曾被误判为需要人工确认，导致流程停在密码输入或登录提交。

修复内容：

- `type_username`
- `type_password`
- `login_submit`

以上动作在受控登录流程中统一归类为 `safe_local_edit`。

运行时将专用登录动作转换为普通 `type/click` 时，也会保留正确风险级别，避免错误继承 `unsafe_mutation`。

主要实现文件：

- `src/aiops_agent/browser/risk.py`
- `src/aiops_agent/browser/agent.py`

## 4. 真实业务验证结果

满足“同一命令至少成功两次”的两个真实任务为：

| Task ID | 状态 | 返回结果 |
| --- | --- | --- |
| `767ae1f3-860e-4560-98b7-b85d40342117` | success | `锦乔生物科技有限公司经办人` |
| `a4712966-cb80-4df1-ba2b-a300c20c04de` | success | `当前已分配岗位中的岗位名称：锦乔生物科技有限公司经办人。` |

排查过程中出现过的典型失败及对应修复：

- Query 消歧完成后，仍使用旧的用户编号点击逻辑，未打开岗位弹窗。
- 第一版行选择修复范围过宽，导致查询前选择表格行。
- 登录密码动作错误进入人工确认。
- 已进入已分配岗位页面，但结果提取器只识别中文标题，未识别英文标题。

这些失败均用于定位问题，没有被计入成功验收。

## 5. Save Skill 参数化问题

### 5.1 现象

真实成功任务的 canonical trace 中实际存在两个输入动作：

```text
授权单位搜索输入框 = 101-51013200_内部客户
userName           = U0002865
```

但保存后的 Skill 只生成一个 `username` 参数，并把两个原始值都放到该参数 examples 中：

```json
{
  "name": "username",
  "examples": [
    "101-51013200_内部客户",
    "U0002865"
  ]
}
```

这会导致两个 type 动作都渲染为 `{{username}}`，Skill 无法复用。

### 5.2 根因

参数生成器把 `target_hint`、`target_id` 和 `expected_outcome` 拼成一个字符串，再按 alias 顺序匹配字段。

授权单位动作的结果描述包含：

```text
按用户指令在授权单位下拉搜索框中输入...
```

其中“用户”先命中了 username 的宽泛别名，导致授权单位被误分类为 username。

另一个缺口是原始目标采用：

```text
在授权单位展开授权单位下拉列表，输入“...”
```

旧解析器只支持：

```text
在授权单位中输入“...”
```

因此无法从原始目标补充授权单位参数。

### 5.3 通用修复

本次不是针对单个 ifinance Skill 写死，而是修改通用 Skill 生成和回放链路：

1. 字段自身上下文与通用 `expected_outcome` 分开匹配。
2. 从结果描述中移除“按用户指令”等非字段语义文本。
3. 新增通用字段集合，包括授权单位、所属单位、用户名、用户名称、登录名称等。
4. 支持“展开/打开下拉列表后输入”的目标句式。
5. 清理“之后在”“然后在”等连接词，保证合成字段提示仍为“用户名”。
6. 生成阶段和 Skill 回放阶段使用一致的参数识别能力。

主要实现文件：

- `src/aiops_agent/browser/skills/generator.py`
- `src/aiops_agent/browser/skills/renderer.py`

修复后生成结果：

```json
{
  "inputs": [
    {
      "name": "company_name",
      "examples": ["101-51013200_内部客户"]
    },
    {
      "name": "username",
      "examples": ["U0002865"]
    }
  ]
}
```

对应动作模板：

```text
授权单位搜索输入框 -> {{company_name}}
userName            -> {{username}}
```

## 6. 保存后的 Skill 自动匹配

排查发现 `create_controller()` 虽然创建了 `WebSkillMatcher`，但主规划器使用的是未传 matcher 的裸 `PlanningService()`。

后果是：

- `/skill <name>` 可以显式调用 Skill。
- 相同自然语言任务却不会自动命中已保存 Skill。
- 任务仍会进入 `auto_plan=true` 的普通 LLM 浏览器流程。

修复后主规划器使用：

```python
PlanningService(web_skill_matcher=web_skill_matcher)
```

现在同一条自然语言任务可以生成：

```text
skill_name = ifinance-assigned-role
auto_plan = false
actions_count = 19
company_name = 101-51013200_内部客户
username = U0002865
```

主要实现文件：

- `src/aiops_agent/cli.py`
- `src/aiops_agent/planning.py`

## 7. 已生成的 Skill

使用真实成功任务 `a4712966-cb80-4df1-ba2b-a300c20c04de` 重新生成：

```text
storage/web_skills/ifinance-assigned-role/
```

生成信息：

- Skill 名称：`ifinance-assigned-role`
- 来源任务：`a4712966-cb80-4df1-ba2b-a300c20c04de`
- 参数：`company_name`、`username`
- 固定动作数：19
- `requires_login=true`
- `auto_plan=false`
- `fallback_to_llm_once=true`

显式调用示例：

```text
/skill ifinance-assigned-role company_name=101-51013200_内部客户 username=U0002865 credential_ref=ifinance-check-admin
```

自然语言原命令也会自动匹配该 Skill。

## 8. Skill 改名能力

新增通用 chat 命令：

```text
/rename-skill <old-name> <new-name>
```

示例：

```text
/rename-skill ifinance-assigned-role ifinance-query-assigned-role
```

改名不是只修改目录名称，而是执行完整一致性更新：

- 校验旧 Skill 存在且可正常加载。
- 校验新名称格式。
- 拒绝覆盖已存在的目标 Skill。
- 复制并保留额外 assets、references 和其他附加文件。
- 更新 `SKILL.md` frontmatter 中的 `name`。
- 更新 `assets/workflow.json` 中的 `skill_name`。
- 使用新名称重新加载并验证完整性。
- 验证成功后才删除旧目录。
- 失败时保留旧 Skill，避免改名失败导致 Skill 丢失。

Skill 名称约束：

- 只允许小写字母、数字和连字符。
- 不能以连字符开头或结尾。
- 不能包含连续两个连字符。
- 最长 64 个字符。

主要实现文件：

- `src/aiops_agent/browser/skills/store.py`
- `src/aiops_agent/agent/controller.py`
- `src/aiops_agent/chat.py`

## 9. 测试覆盖

本次新增或增强的测试主要位于：

- `tests/test_browser_workflow.py`
- `tests/test_playwright_tool_frames.py`
- `tests/test_browser_risk.py`
- `tests/test_web_skills.py`
- `tests/test_chat.py`
- `tests/test_agent_flow.py`
- `tests/test_langgraph_runtime.py`

关键覆盖内容：

- Select2 搜索框优先于普通页面输入框。
- 点击 Select2 高亮第一项。
- `target_id` 优先于通用 Query alias。
- 查询条件按钮优先于其他 Query 按钮。
- jqGrid 第一条业务数据选择。
- 遮罩层拦截情况下的行点击。
- 登录账号、密码、提交动作风险分类。
- 授权单位和用户名保存为两个独立参数。
- Skill 自动推断两个参数并正确渲染。
- `create_controller()` 将 matcher 接入主规划器。
- Skill 改名同步更新内部身份。
- Skill 改名保留附加 assets。
- 已存在目标名称时拒绝覆盖并保留源 Skill。
- chat `/rename-skill` 命令解析和错误提示。

最终相关测试套件结果：

```text
144 passed
```

此前浏览器工作流专项结果：

```text
tests/test_browser_workflow.py: 59 passed
tests/test_browser_risk.py: 8 passed
关键 Playwright 用例: 5 passed
```

一次扩大范围的 Playwright 测试运行出现 3 个日期控件/遮罩相关失败，结果为 `110 passed, 3 failed`。这些失败不在本轮 Save Skill 和改名改动路径内，未通过修改无关逻辑掩盖。最终 Skill、chat、规划器和浏览器工作流相关的精确套件均通过。

## 10. 最后一次真实回放的环境状态

保存后的 Skill 已通过真实任务记录确认：

```text
skill_name = ifinance-assigned-role
auto_plan = false
skill_parameters = {
  company_name: 101-51013200_内部客户,
  username: U0002865
}
```

但最后一次真实回放未进入业务页面：

| Task ID | 状态 | 原因 |
| --- | --- | --- |
| `018eb917-ab40-4572-914e-642926af1f28` | failed | macOS sandbox 拒绝 Chromium Mach port 注册，浏览器未启动 |
| `a9323574-f628-4c2a-94da-51f5271ae184` | blocked | 沙箱外浏览器已启动，但 ifinance 登录地址连续 3 次 `Page.goto` 5 秒超时 |

任务 `a9323574-f628-4c2a-94da-51f5271ae184` 的浏览器停在：

```text
chrome-error://chromewebdata/
```

失败分类为：

```text
site_unavailable
```

这次失败发生在 Skill 第一个 `open_url` 动作，尚未执行任何授权单位、用户名或岗位查询动作。因此它证明了 Skill 已被正确匹配和参数化，但不能算业务回放成功。业务正确性仍以前述两次真实成功任务为验收依据。

## 11. 本次主要修改文件

浏览器稳定化：

- `src/aiops_agent/browser/agent.py`
- `src/aiops_agent/browser/playwright_tool.py`
- `src/aiops_agent/browser/risk.py`

Skill 生成、匹配、渲染、调用和存储：

- `src/aiops_agent/browser/skills/generator.py`
- `src/aiops_agent/browser/skills/renderer.py`
- `src/aiops_agent/browser/skills/matcher.py`
- `src/aiops_agent/browser/skills/invocation.py`
- `src/aiops_agent/browser/skills/store.py`

主流程和 chat：

- `src/aiops_agent/agent/controller.py`
- `src/aiops_agent/browser/subgraph.py`
- `src/aiops_agent/chat.py`
- `src/aiops_agent/cli.py`

测试：

- `tests/test_browser_workflow.py`
- `tests/test_playwright_tool_frames.py`
- `tests/test_browser_risk.py`
- `tests/test_web_skills.py`
- `tests/test_chat.py`
- `tests/test_agent_flow.py`
- `tests/test_langgraph_runtime.py`

## 12. 当前可用命令

保存最近一次成功网页任务：

```text
/save-skill [skill-name]
```

查看 Skill：

```text
/skills
/skill <skill-name> --help
```

显式执行 Skill：

```text
/skill <skill-name> key=value ...
```

改名 Skill：

```text
/rename-skill <old-name> <new-name>
```

删除 Skill：

```text
/delete-skill <skill-name> [--yes]
/remove-skill <skill-name> [--yes]
```

## 13. 后续建议

1. ifinance 网络恢复后，重新执行一次 `ifinance-assigned-role` Skill，完成固定动作流的在线终态验证。
2. 单独排查剩余 3 个 Playwright 日期控件/遮罩测试，避免与本轮 Skill 修改混合处理。
3. 后续增加 `/copy-skill` 或 Skill 导出/导入时，复用本次 rename 的“复制、更新身份、重新验证、最后删除源”的一致性模式。
4. 对更多下拉组件和不同字段组合增加参数化回归样本，继续验证 Save Skill 的通用性。

