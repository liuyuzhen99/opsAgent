# Skill 参数化生成改造总结（2026-05-28）

## 背景

原来的 `/save-skill` 会把一次成功的 `web_action` 轨迹直接沉淀为固定 workflow，并把 `type/select/press` 里的动态值简单替换成 `input_value`、`input_value_2`。

这个方式的问题是：

- 下次调用时，系统不知道 `input_value` 实际代表什么业务含义。
- 日期范围、用户名、授权单位、岗位等参数无法稳定从新输入中抽取。
- 必填参数缺失时，skill 可能参与评分但没有可执行 actions。
- `finish.value` 可能把上一次查询结果保存进 workflow，造成动态结果污染。

本次改造目标是把 action 沉淀升级为：

```text
workflow + typed 参数 schema + 参数化决策记录 + 固定值策略
```

## 设计原则

- 不让 LLM 自由决定参数 schema，第一版使用可测试的规则和白名单类型。
- 保存 skill 时自动判断哪些值是可变参数，哪些值是固定流程值。
- 命中 skill 时根据 typed schema 从用户输入中抽取参数。
- 必填参数缺失时不命中 skill，避免返回空 actions。
- 保持旧版 `input_value` skill 的兼容性。

## 主要实现

### 1. 保存阶段：语义化参数推断

修改文件：

- `src/aiops_agent/browser/skills/generator.py`
- `src/aiops_agent/browser/skills/models.py`

新增能力：

- 保存 workflow 时生成 typed inputs，例如：

```json
{
  "name": "start_date",
  "type": "date",
  "required": true,
  "source": "user_goal",
  "aliases": ["开始日期", "起始日期", "开始时间", "起始时间", "from", "start", "start_date"],
  "examples": ["2026-05-13"],
  "original_value": "2026-05-13"
}
```

- 支持通用参数类型：
  - `start_date`
  - `end_date`
  - `username`
  - `company_name`
  - `role`
  - `department`
  - `display_name`
  - `email`
  - `amount`
  - `batch_no`
  - `account_no`

- 参数化决策会写入 workflow：

```json
"parameterization_decisions": [
  {
    "action_key": "field.start",
    "field_hint": "指令创建日期： * 至： *",
    "original_value": "2026-05-13",
    "decision": "variable",
    "param_name": "start_date",
    "param_type": "date",
    "confidence": 0.95,
    "reason": "date-like value in date-like field"
  }
]
```

- `finish.value` 中的上一次查询结果不再保存，改为记录：

```json
{
  "decision": "dynamic_result/excluded",
  "original_value": "[dynamic result omitted]"
}
```

### 2. 命中阶段：按 schema 抽取参数

修改文件：

- `src/aiops_agent/browser/skills/renderer.py`

新增能力：

- 支持从用户输入中抽取日期范围：

```text
2026-05-11到2026-05-28
从2026-05-11至2026-05-28
2026-05-11 - 2026-05-28
```

抽取结果：

```json
{
  "start_date": "2026-05-11",
  "end_date": "2026-05-28"
}
```

- 支持显式参数表达：

```text
开始日期为2026-05-11
结束日期为2026-05-28
```

- 保留旧逻辑：
  - 先读 `entities.workflow_fields`
  - 再从用户输入抽取
  - 最后用 examples 兜底

### 3. 匹配阶段：缺参不命中

修改文件：

- `src/aiops_agent/browser/skills/matcher.py`

调整逻辑：

- 不降低默认阈值 `0.75`。
- 必填参数缺失时直接返回 `None`。
- 不再返回 `actions=[]` 的半命中结果。

判断命中的关键字段仍然是：

```text
skill_name 有值
auto_plan=false
skill_parameters 有值
actions_len > 0
```

### 4. Chat 保存预览

修改文件：

- `src/aiops_agent/chat.py`

`/save-skill` 保存后会输出参数预览和固定值预览，例如：

```text
参数预览:
- start_date date 原值=2026-05-13
- end_date date 原值=2026-05-28

固定值:
- 查询=查询 confidence=0.35
```

## 测试覆盖

修改文件：

- `tests/test_web_skills.py`

新增覆盖：

- 日期范围 action 会保存为 `start_date/end_date`。
- `finish.value` 不保存旧查询结果。
- 新日期范围输入可以命中同一个 skill。
- 缺少必填参数时不命中 skill。
- 旧版 `input_value` skill 仍然兼容。
- `/save-skill` 输出参数预览。

验证结果：

```text
tests/test_web_skills.py: 11 passed
全量测试: 184 passed, 9 skipped, 2 warnings
git diff --check: clean
```

## 真实轨迹验证

使用真实源任务：

```text
80208269-3c4c-4a38-8f43-de3311559ee6
```

在临时目录生成 skill 后确认：

```text
inputs = ["start_date", "end_date"]
matched = true
params = {
  "start_date": "2026-05-11",
  "end_date": "2026-05-28"
}
actions = 13
```

## 使用说明

已有旧 skill 不会自动迁移。需要重新保存一次：

```text
/save-skill ifinance-person-payment-search-bydate
```

之后再执行类似任务：

```text
登录财司系统，点击银企平台->银企指令控制台->对私指令查询，将时间范围设置为2026-05-11到2026-05-28，然后点击查询
```

应能命中新版 skill，并在任务 JSON 中看到：

```text
skill_name=ifinance-person-payment-search-bydate
auto_plan=false
skill_parameters.start_date=2026-05-11
skill_parameters.end_date=2026-05-28
actions_len > 0
```

## 后续可扩展方向

- 增加 `/save-skill --review`，允许保存前手动把字段改为 variable 或 constant。
- 给低置信度 constant 提示用户确认。
- 扩展更多业务参数类型，例如 `currency`、`status`、`instruction_no`。
- 让 LLM 只作为“参数命名建议”辅助，但最终仍经过白名单和 schema 校验。
