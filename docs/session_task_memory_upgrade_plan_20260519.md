# 当前 Session 内 Task Memory + Session Memory 改造计划 v2

## Summary

保留完整 task JSON 持久化、`ContextCompressor.compress(session, task)` 调用点、当前 session 内检索策略。  
结构化 memory 全部由规则生成。`session.rolling_summary` 也由规则生成。  
LLM 只用于生成 `session.summary` 的长期自然语言摘要；LLM 不可用、接口缺失、返回空值或异常时，`summary` 回退到规则摘要。

不做跨 session memory，不做全局 task index，不引入 SQLite/向量检索。`ResultSummarizer` 本次不改。

## Design Principles

- `ContextCompressor` 是 session memory 的唯一写入入口。
- Controller 只读取 memory，不直接维护结构化 memory。
- `task.report` 继续由现有 `ResultSummarizer` 生成。
- `storage/tasks/<task_id>.json` 继续保存完整 task。
- `session.task_index` 只保存当前 session 内轻量索引。
- `retrieve()` 只返回 memory 和匹配到的 `task_id`，不直接读取完整 task JSON。
- LLM prompt 只接收经过规则抽取、截断、脱敏后的 memory facts，不接收完整 task JSON。

## Data Model Changes

### `sessions/models.py`

新增 dataclass：

```python
@dataclass(slots=True)
class ShortTermTurn:
    task_id: str
    intent: str
    status: str
    input: str
    report: str
    created_at: str


@dataclass(slots=True)
class PageMemory:
    task_id: str
    url: str
    title: str
    page_type: str
    observed_at: str


@dataclass(slots=True)
class BrowserMemory:
    recent_pages: list[PageMemory] = field(default_factory=list)
    state_path: str = ""
    last_url: str = ""
    last_page_type: str = ""
    last_success_task_id: str = ""
    last_success_site_key: str = ""


@dataclass(slots=True)
class QATurn:
    task_id: str
    question: str
    answer: str
    created_at: str


@dataclass(slots=True)
class SessionTaskIndexEntry:
    task_id: str
    intent: str
    status: str
    system: str = ""
    env: str = ""
    site_key: str = ""
    url: str = ""
    title: str = ""
    summary: str = ""
    created_at: str = ""
```

`AgentSession` 新增字段：

```python
short_term: list[ShortTermTurn] = field(default_factory=list)
browser_memory: BrowserMemory = field(default_factory=BrowserMemory)
qa_memory: list[QATurn] = field(default_factory=list)
task_index: list[SessionTaskIndexEntry] = field(default_factory=list)
```

保留兼容字段：

```python
summary: str = ""
rolling_summary: str = ""
recent_observations: list[dict[str, str]] = field(default_factory=list)
metadata: dict[str, str] = field(default_factory=dict)
```

## Migration Changes

### `storage/session_store.py`

`load()` 增加显式 migration 和反序列化 helper，不能只用 `setdefault`。

需要支持：

- 旧 session JSON 缺失新字段时补默认值。
- 嵌套 dict 转回 dataclass，而不是保留为 dict。
- 从旧 `metadata["qa_turns"]` 初始化 `qa_memory`。
- 从旧 `recent_observations` 初始化 `browser_memory.recent_pages`。
- 从旧 metadata 初始化：
  - `browser_memory.last_url`
  - `browser_memory.last_page_type`
  - `browser_memory.state_path`
  - `browser_memory.last_success_task_id`
- 容忍坏数据：
  - `metadata["qa_turns"]` 非合法 JSON 时忽略。
  - `recent_observations` 缺字段时用空字符串。
  - 新字段若类型错误，回退默认值。

保存继续使用：

```python
json.dump(asdict(session), ...)
```

## Context Compressor Changes

### `agent/context.py`

保留：

```python
class ContextCompressor:
    def compress(self, session: AgentSession, task: Task) -> AgentSession:
        ...
```

构造函数新增：

```python
def __init__(self, llm_provider: BaseLLMProvider | None = None):
    self.llm_provider = llm_provider
```

内部拆分：

```python
extract_memory_facts(session, task) -> dict[str, Any]
apply_memory_facts(session, task, facts) -> AgentSession
render_rolling_summary(session) -> str
summarize_session(session, facts) -> str
retrieve(session, intent, query, limit=5) -> dict[str, Any]
```

### `compress()` 流程

1. `facts = extract_memory_facts(session, task)`
2. `session = apply_memory_facts(session, task, facts)`
3. `session.rolling_summary = render_rolling_summary(session)`
4. `session.summary = summarize_session(session, facts)`
5. return session

### 结构化 memory 规则

`short_term`：

- 每个 task 结束后追加一条。
- 保存最近 10 条。
- `input`、`report` 做长度截断。
- `knowledge_write` 可继续使用安全摘要，不把完整知识库内容塞入 memory。

`task_index`：

- 每个 task 结束后追加一条。
- 保存最近 50 条。
- 字段从 `task.intent`、`task.status`、`task.entities`、`task.result.data.last_observation`、`task.report` 抽取。
- 用于当前 session 内检索排序。

`qa_memory`：

- 仅当 `task.intent == "ops_qa"` 且 `task.status == "success"` 且存在 answer 时追加。
- 保存最近 5 条。
- 同步写 legacy `metadata["qa_turns"]`。
- Controller 不再直接 append QA。

`browser_memory`：

- 从 `task.result.data.last_observation` 更新 recent pages。
- `recent_pages` 保存最近 5 条。
- 从 `task.result.data.session_state_path` 更新 `state_path`。
- 成功 `web_action` 更新：
  - `last_success_task_id`
  - `last_success_site_key`
- 同步写 legacy：
  - `recent_observations`
  - `metadata["last_url"]`
  - `metadata["last_page_type"]`
  - `metadata["browser_state_path"]`
  - `metadata["browser_last_success_task_id"]`

### `rolling_summary`

`render_rolling_summary()` 永远规则生成。

建议内容：

- 最近 task 状态。
- 最近 QA 简要。
- 最近 browser 页面。
- 最近成功 web task。
- 当前 session 最近完成动作概览。

不得调用 LLM。

### `summary`

`summarize_session()` 优先调用 LLM，但必须软依赖：

```python
provider = self.llm_provider
if provider and getattr(provider, "enabled", False) and hasattr(provider, "summarize_session_memory"):
    try:
        summary = provider.summarize_session_memory(sanitized_facts)
        if summary.strip():
            return summary.strip()
    except Exception:
        pass
return self._rule_summary(session)
```

`sanitized_facts` 要：

- 不包含完整 task JSON。
- 不包含 browser state 本地路径。
- 不包含密码、token、credential。
- 对 question、answer、report、url/title 做长度限制。

## LLM Changes

### `llm/base.py`

`BaseLLMProvider` 新增接口：

```python
def summarize_session_memory(self, memory_facts: dict[str, Any]) -> str:
    raise NotImplementedError
```

但 `ContextCompressor` 调用时必须兼容旧 fake provider，不假设一定存在。

### `llm/langchain_provider.py`

实现：

```python
def summarize_session_memory(self, memory_facts: dict[str, Any]) -> str:
    ...
```

要求：

- LLM disabled 时抛 `LLMError`。
- Prompt 只接收规则 facts。
- 返回纯文本 summary。
- 空字符串视为失败，由 `ContextCompressor` fallback。
- 不要求 JSON 输出。

Prompt 目标：

- 概括当前 session 用户目标、已完成事项、仍可延续的上下文。
- 不编造。
- 不输出敏感路径或 credential。
- 简短，适合作为长期 session 摘要。

## Controller Changes

### `_task_plan_node()`

优先使用：

```python
session.qa_memory[-5:]
```

转换为现有 `conversation_history` 格式：

```python
[{"question": turn.question, "answer": turn.answer}, ...]
```

若 `qa_memory` 为空，fallback 到旧 `metadata["qa_turns"]`。

### `_persist_audit_node()`

删除直接维护 `metadata["qa_turns"]` 的逻辑。  
继续调用：

```python
session = self.context_compressor.compress(session, task)
```

### `save_web_skill()`

优先读取：

```python
session.browser_memory.last_success_task_id
```

fallback：

```python
session.metadata["browser_last_success_task_id"]
```

实现时用安全访问，兼容测试里的 `SimpleNamespace`：

```python
browser_memory = getattr(session, "browser_memory", None)
task_id = getattr(browser_memory, "last_success_task_id", None) or session.metadata.get("browser_last_success_task_id")
```

## CLI Changes

### `cli.py`

`create_controller()` 注入 LLM provider：

```python
context_compressor=ContextCompressor(llm_provider=provider)
```

不改：

```python
summarizer=ResultSummarizer()
```

## Retrieval Design

### `ContextCompressor.retrieve(session, intent, query, limit=5)`

只读取当前 `AgentSession` 内 memory，不扫描 session 文件，不读完整 task JSON。

返回结构建议：

```python
{
    "summary": session.summary,
    "rolling_summary": session.rolling_summary,
    "qa_memory": [...],
    "short_term": [...],
    "browser_memory": {...},
    "task_matches": [...],
}
```

按 intent 策略：

- `ops_qa`: 最近 `qa_memory` + 相关 `short_term`
- `knowledge_write`: 最近 QA + 最近 short term
- `web_action`: `browser_memory` + 当前 session 最近成功 web task index
- `inspection`: 当前 session 内同 `system/env` 的 task index 优先
- `general_chat`: `summary` + 最近 short term

排序规则：

- 同 intent 优先。
- 同 `system/env` 优先。
- 同 `site_key` 优先。
- 越新越优先。
- 返回最多 `limit` 条 task index match。

需要完整 task 详情时，由调用方根据返回的 `task_id` 使用 `TaskManager.load()` 读取。

## Test Plan

### Migration

- 旧 session JSON 缺新字段时可加载。
- load 后嵌套字段是 dataclass，不是 dict。
- 旧 `metadata["qa_turns"]` 可初始化为 `qa_memory`。
- 非法 `qa_turns` JSON 不会导致 load 失败。
- 旧 `recent_observations` 可初始化为 `browser_memory.recent_pages`。
- 旧 browser metadata 可初始化为 `browser_memory`。
- 保存后再次 load，结构化 memory 类型和值保持正确。

### ContextCompressor

- 每次任务结束更新 `short_term`，最多 10 条。
- 每次任务结束更新 `task_index`，最多 50 条。
- `ops_qa success` 更新 `qa_memory`，最多 5 条。
- `ops_qa failure` 不更新 `qa_memory`。
- `qa_memory` 更新时同步 legacy `metadata["qa_turns"]`。
- `web_action` 更新 `browser_memory`。
- `web_action success` 更新最近成功 task id。
- browser memory 同步 legacy metadata fallback 字段。
- `rolling_summary` 始终规则生成。
- LLM enabled 时，仅 `summary` 使用 LLM 输出。
- LLM disabled 时，`summary` 使用规则 fallback。
- LLM 接口缺失时，`summary` 使用规则 fallback。
- LLM 返回空值时，`summary` 使用规则 fallback。
- LLM 抛异常时，`summary` 使用规则 fallback。
- 结构化 memory 不受 LLM 输出影响。

### Retrieval

- `retrieve()` 只使用当前 session memory。
- `retrieve()` 不读取其他 session 文件。
- `retrieve()` 不直接读取完整 task JSON。
- 同 intent entry 优先。
- `inspection` 同 `system/env` entry 优先。
- `web_action` 同 `site_key` 和最近成功 web task 优先。
- 返回结果包含 matched `task_id`，供调用方读取完整 task。

### Controller Regression

- `_task_plan_node()` 优先使用 `session.qa_memory`。
- `qa_memory` 为空时 fallback 到 `metadata["qa_turns"]`。
- `_persist_audit_node()` 不再直接写 `metadata["qa_turns"]`。
- `save_web_skill()` 优先使用 `browser_memory.last_success_task_id`，fallback 旧 metadata。
- `SimpleNamespace` session mock 不报错。

### Existing Regression

继续跑：

- `tests/test_agent_flow.py`
- `tests/test_phase2_resume.py`
- `tests/test_chat.py`
- `tests/test_web_skills.py`
- `tests/test_knowledge_tool.py`

## Implementation Order

1. 修改 `sessions/models.py`，新增 memory dataclass 和 `AgentSession` 字段。
2. 修改 `storage/session_store.py`，实现显式 migration/from-dict。
3. 重写 `agent/context.py`，集中维护结构化 memory、legacy fallback、rolling summary、LLM summary fallback、retrieve。
4. 修改 `llm/base.py` 和 `llm/langchain_provider.py`，新增 `summarize_session_memory()`。
5. 修改 `agent/controller.py`，读取 `qa_memory` 和 `browser_memory`，删除 QA metadata 直接写入。
6. 修改 `cli.py`，注入 `ContextCompressor(llm_provider=provider)`。
7. 增加 migration、compressor、retrieval、LLM fallback 测试。
8. 跑回归测试并修复兼容问题。

## Implementation Record - 2026-05-19

### Completed Changes

- `src/aiops_agent/sessions/models.py`
  - 新增 `ShortTermTurn`、`PageMemory`、`BrowserMemory`、`QATurn`、`SessionTaskIndexEntry`。
  - `AgentSession` 新增 `short_term`、`browser_memory`、`qa_memory`、`task_index`。
  - 保留 `summary`、`rolling_summary`、`recent_observations`、`metadata` 兼容字段。

- `src/aiops_agent/storage/session_store.py`
  - 将 `load()` 改为显式 from-dict/migration 流程。
  - 支持新结构嵌套 dict 反序列化为 dataclass。
  - 支持从旧 `metadata["qa_turns"]`、`recent_observations`、browser legacy metadata 初始化新 memory。
  - 对非法 `qa_turns` JSON、缺字段 observation、错误类型新字段做容错回退。
  - `save()` 继续使用 `json.dump(asdict(session), ...)`。

- `src/aiops_agent/agent/context.py`
  - 保留 `ContextCompressor.compress(session, task)` 调用点。
  - 新增 `llm_provider` 软依赖注入。
  - 拆分并实现：
    - `extract_memory_facts()`
    - `apply_memory_facts()`
    - `render_rolling_summary()`
    - `summarize_session()`
    - `retrieve()`
  - 结构化 memory 全部由规则生成：
    - `short_term` 最近 10 条。
    - `task_index` 最近 50 条。
    - `qa_memory` 仅记录成功 `ops_qa`，最近 5 条。
    - `browser_memory` 记录最近页面、state path、最近成功 web task/site。
  - 同步维护 legacy fallback：
    - `metadata["qa_turns"]`
    - `recent_observations`
    - `metadata["last_url"]`
    - `metadata["last_page_type"]`
    - `metadata["browser_state_path"]`
    - `metadata["browser_last_success_task_id"]`
    - `metadata["browser_last_success_site_key"]`
  - `rolling_summary` 始终规则生成。
  - `summary` 优先调用 LLM 的 `summarize_session_memory()`，接口缺失、disabled、空值、异常时回退规则摘要。
  - LLM facts 做截断、敏感字段过滤和敏感文本脱敏，不传完整 task JSON，不传 browser state 本地路径。
  - `retrieve()` 只读取当前 `AgentSession` 内 memory，返回 memory 快照和 `task_matches` 的 `task_id`。

- `src/aiops_agent/llm/base.py`
  - `BaseLLMProvider` 新增 `summarize_session_memory(memory_facts)` 接口。

- `src/aiops_agent/llm/langchain_provider.py`
  - 实现 `summarize_session_memory()`。
  - LLM disabled 时抛 `LLMError`。
  - Prompt 只接收规则抽取后的 memory facts，返回纯文本摘要。
  - 空响应抛 `LLMError`，由 `ContextCompressor` fallback。

- `src/aiops_agent/agent/controller.py`
  - `_task_plan_node()` 优先读取 `session.qa_memory[-5:]`，为空时 fallback 到旧 `metadata["qa_turns"]`。
  - `_persist_audit_node()` 删除直接维护 `metadata["qa_turns"]` 的逻辑，只调用 `ContextCompressor.compress()`。
  - `save_web_skill()` 优先读取 `session.browser_memory.last_success_task_id`，再 fallback 到旧 metadata，并兼容 `SimpleNamespace` mock。

- `src/aiops_agent/cli.py`
  - `create_controller()` 注入 `ContextCompressor(llm_provider=provider)`。
  - 未改动 `ResultSummarizer()`。

- `tests/test_session_memory.py`
  - 新增专门测试覆盖 migration、compressor、retrieval、LLM fallback、controller regression。

### Verification

- 已运行全量测试：

```bash
.venv/bin/python -B -m pytest
```

- 结果：

```text
152 passed, 6 skipped, 2 warnings
```

### Notes

- 本次未实现跨 session memory、全局 task index、SQLite 或向量检索。
- `ResultSummarizer` 未改。
- 完整 task JSON 仍由 `storage/tasks/<task_id>.json` 保存。
- `retrieve()` 不扫描 session 文件，也不读取完整 task JSON；需要完整详情时由调用方根据返回的 `task_id` 使用 `TaskManager.load()`。
- 工作区中原本已有 `storage/audit/events.jsonl` 修改，本次改造未依赖该文件。
