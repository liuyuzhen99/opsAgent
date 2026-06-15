# Python Typing / Collections / Pydantic 学习笔记

这份笔记配合本仓库的浏览器动作模型阅读，重点不是背语法，而是理解类型如何保护工程边界。

## 1. 类型标注：把自由字符串收成受控协议

`src/aiops_agent/browser/models.py` 里的 `BrowserActionType` 使用 `Literal[...]` 定义浏览器动作白名单。这样 `BrowserAction.type` 不再是任意 `str`，而是受控动作协议：

```python
BrowserActionType = Literal["open_url", "click", "hover", "type", ...]

@dataclass(slots=True)
class BrowserAction:
    type: BrowserActionType
```

这类类型适合描述“枚举式字符串协议”：动作名、状态名、workflow 名。它不会在 dataclass 运行时自动校验输入，但能让编辑器、类型检查器和调用者更早发现拼写错误。

## 2. Collections：默认值要用 factory

内部状态模型大量使用：

```python
artifacts: list[TaskArtifact] = field(default_factory=list)
metadata: dict[str, Any] = field(default_factory=dict)
```

原因是 `list` / `dict` 是可变对象。直接写 `metadata: dict = {}` 会让多个实例共享同一个对象，导致状态互相污染。`default_factory` 会为每个实例创建新的集合。

集合类型可以按可信度分层：

- `list[str]` / `dict[str, str]`：结构明确，适合核心业务字段。
- `list[BrowserAction]`：已经解析过的内部对象集合。
- `dict[str, Any]`：JSON-like 边界，适合外部 payload、审计数据、临时兼容层。

## 3. dataclass vs Pydantic

仓库里有一个清晰分工：

- `dataclass` 用于内部可信状态，例如 `BrowserAction`、`BrowserTaskSpec`、`Task`。
- `Pydantic BaseModel` 用于外部输入和配置，例如 `BrowserPlannerOutput`、`BrowserSiteConfig`。

内部状态追求轻量、可序列化、低开销；外部输入需要强校验、默认值、错误报告和拒绝未知字段。

## 4. Pydantic v2：字段校验和跨字段校验

`BrowserPlannerOutput` 用 `ConfigDict(extra="forbid")` 拒绝多余字段，用 `Literal` 限制动作名，用 `Field` 限制数值范围：

```python
timeout_ms: int = Field(default=5000, ge=100, le=60000)
```

`model_validator(mode="after")` 适合做跨字段校验。例如：

- `open_url` 必须是绝对 `http(s)` URL。
- `type` / `select` 必须有 `value`。
- `click` / `hover` / `type` 等交互动作必须有 `target_hint` 或 `target_id`。

这类校验属于“输入进入系统前收窄”，能防止 LLM 或配置文件把坏数据带进执行层。

## 5. 本次练习：新增 `hover`

这次改造贯通了四层：

- 类型层：`BrowserActionType` 增加 `"hover"`。
- Pydantic 层：`BrowserPlannerOutput` 允许 `"hover"`，并要求目标字段。
- 执行层：`PlaywrightBrowserTool` 调用 `locator.hover(...)`。
- 风险层：`RiskEvaluator` 把 `hover` 视为安全本地交互。

对应测试放在 `tests/test_browser_site_config.py`：

- 非法：`{"type": "hover"}` 缺少目标，应触发 `ValidationError`。
- 合法：`{"type": "hover", "target_hint": "更多操作"}` 可转成 `BrowserAction`。

## 6. 判断是否该收紧类型

收紧类型前先问三个问题：

1. 这个字段是不是协议的一部分？如果是，优先 `Literal` 或专门类型别名。
2. 这个字段是不是外部 JSON 边界？如果是，先用 Pydantic 校验，再进入内部模型。
3. 这个字段是不是仍在兼容多种历史形状？如果是，先别急着去掉 `Any`，可以在边界函数里逐步转换。

一个实用原则：核心模型要表达意图，边界层要承认混乱，然后把混乱收窄。
