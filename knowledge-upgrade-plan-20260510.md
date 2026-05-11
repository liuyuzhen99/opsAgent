# Knowledge RAG 升级计划 20260510

## 背景

基于原始计划（snazzy-finding-swan.md）的 review 结论，修订后的落地方案。原始计划技术选型正确（RRF、LLM-as-judge、session qa_turns），主要问题集中在：

- **ContextCompressor 职责混入**：原计划让 `ContextCompressor.compress()` 写 qa_turns，但该方法是纯 web/browser 上下文设计，ops_qa 的 result 结构完全不同
- **confidence hardcode**：`retriever.py` 中 `confidence=1.0` 是占位符，评估引入后应从评估分数推导
- **流式输出被低估**：`ToolExecutionResult` 是同步 dict，打通 tool→controller→chat 的流式链路需要较大架构改动，本轮不做
- **planning.py 遗留占位文字**：ops_qa plan 仍有 MVP 阶段的 `"知识检索工具将在后续阶段接入"` 等文字
- **RRF 去重依赖 chunk identity**：indexer 未写入 chunk_index，需先补充
- **Evaluator 默认关闭**：LLM-as-judge 每次多一次 LLM 调用，应通过 `enable_eval` flag 控制

---

## 文件变更清单

| 文件 | 操作 |
|------|------|
| `src/aiops_agent/config.py` | `KnowledgeConfig` 新增 `enable_eval: bool = False` |
| `src/aiops_agent/knowledge/indexer.py` | `split_docs()` 写入 `chunk_index` 到 metadata，供 RRF 去重 |
| `src/aiops_agent/knowledge/retriever.py` | 新增 `retrieve_hybrid()`, `_rrf_merge()`, `rewrite_query()`, `synthesize_with_history()` |
| `src/aiops_agent/knowledge/engine.py` | hybrid 模式分发、`_get_hybrid()`、接受 `conversation_history`，串联 rewrite + synthesize_with_history |
| `src/aiops_agent/knowledge/evaluator.py` | **新增**：`RAGEvaluator`，faithfulness + relevance 两个指标 |
| `src/aiops_agent/tools/knowledge.py` | `execute()` 接受 `conversation_history`，`KnowledgeAnswer` 新增 `evaluation` 字段 |
| `src/aiops_agent/planning.py` | 修正 ops_qa 占位文字；从 entities 读取 `conversation_history` 注入 params |
| `src/aiops_agent/agent/controller.py` | `_persist_audit_node` 写 qa_turns；`_task_plan_node` 读取并注入 tool params |
| `src/aiops_agent/agent/summarizer.py` | ops_qa 分支展示 evaluation 分数摘要 |
| `tests/test_knowledge_tool.py` | 补充 hybrid 路径、conversation_history 注入测试 |
| `tests/test_knowledge_evaluator.py` | **新增**：faithfulness/relevance mock LLM 打分测试 |

**不做：** 流式输出（需改动 ToolExecutionResult 同步结构，架构影响过大）

---

## 实现步骤

### Step 1：修正 planning.py 占位文字（无依赖，先做）

`ops_qa` plan 的 `steps` 和 `notes` 仍是 MVP 时期的占位内容，在面试中看到这里会很刺眼。

**改动：**
- `steps` 改为实际执行描述：查询改写 → 混合检索 → LLM 合成
- `notes` 删除占位说明
- `success_criteria` 改为"返回知识库检索答案及来源文档"

---

### Step 2：indexer 写入 chunk_index，为 RRF 提供去重 key

`split_docs()` 目前不写 `chunk_index`，RRF 无法用 `(source, chunk_index)` 做唯一标识。

**改动：**
```python
def split_docs(self, docs: list[Document]) -> list[Document]:
    chunks = splitter.split_documents(docs)
    # 按 source 分组，写入 chunk_index
    from collections import defaultdict
    counter: dict[str, int] = defaultdict(int)
    for chunk in chunks:
        src = chunk.metadata.get("source", "")
        chunk.metadata["chunk_index"] = counter[src]
        counter[src] += 1
    return chunks
```

---

### Step 3：混合检索 + RRF（retriever + engine）

**retriever.py 新增：**
```python
def retrieve_hybrid(self, question, bm25, bm25_docs, vector_db) -> list[Document]:
    kw = self.retrieve_keyword(question, bm25, bm25_docs)
    vec = self.retrieve_vector(question, vector_db)
    return self._rrf_merge([kw, vec], k=60)

def _rrf_merge(self, lists: list[list[Document]], k: int = 60) -> list[Document]:
    # RRF score = sum(1/(k + rank))
    # 去重 key = (source, chunk_index)
    scores: dict[str, float] = {}
    identity: dict[str, Document] = {}
    for doc_list in lists:
        for rank, doc in enumerate(doc_list):
            key = f"{doc.metadata.get('source','')}::{doc.metadata.get('chunk_index', 0)}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            identity[key] = doc
    ranked = sorted(scores, key=lambda k: scores[k], reverse=True)
    return [identity[key] for key in ranked[: self.TOP_K]]
```

**engine.py 新增：**
- `_get_hybrid()` 同时返回 `(bm25, bm25_docs, vector_db)`（懒加载）
- `query()` 按 `config.index_mode == "hybrid"` 分支调用 `retrieve_hybrid()`

---

### Step 4：查询改写（retriever）

**retriever.py 新增：**
```python
def rewrite_query(self, question: str, history: list[dict]) -> str:
    if not history:
        return question
    # 构造 prompt 让 LLM 将指代句改写为独立问句
    # 若 LLM 调用失败，fallback 返回原句
```

单轮时 history 为空直接返回原句，不增加延迟。

---

### Step 5：多轮对话上下文

**数据流（修订版，不走 ContextCompressor）：**

1. `controller._persist_audit_node()` — task 完成后，若 `task.intent == "ops_qa"` 且成功，从 `task.result["data"]["answer"]` 提取 `{question, answer}`，追加到 `session.metadata["qa_turns"]`（JSON 字符串，保留最近 5 轮）
2. `controller._task_plan_node()` — `ops_qa` 意图时，读取 `session.metadata.get("qa_turns")` 解析后注入 `task.entities["conversation_history"]`
3. `planning.py` — `ops_qa` plan 从 `entities.get("conversation_history", [])` 读取并写入 `ToolCallSpec.params`
4. `KnowledgeTool.execute()` — 读取 `params.get("conversation_history", [])`，传入 `engine.query()`
5. `KnowledgeEngine.query()` — 先调 `rewrite_query()`（Step 4），再检索，再调 `synthesize_with_history()`

**注意：** `AgentSession.metadata` 类型是 `dict[str, str]`，qa_turns 存为 `json.dumps(list)` 字符串，读取时 `json.loads()`。

---

### Step 6：RAG 评估框架（默认关闭）

**新增 `knowledge/evaluator.py`：**
```python
@dataclass
class EvalResult:
    faithfulness: float   # 答案主张是否有文档支撑
    relevance: float      # 答案是否回答了问题
    confidence: float     # = (faithfulness + relevance) / 2

class RAGEvaluator:
    def evaluate(self, question, answer, docs) -> EvalResult: ...
```

**confidence 替换：**
- `KnowledgeAnswer.confidence` 由 `EvalResult.confidence` 赋值（当 `enable_eval=True`）
- `enable_eval=False` 时保持原有逻辑（有 docs → 1.0，无 docs → 0.0）

**触发条件：** `KnowledgeConfig.enable_eval = True` 或 CLI `knowledge eval` 子命令

---

### Step 7：可观测性日志

**每次 query 在 engine.py 记录结构化日志：**
```json
{
  "query_original": "...",
  "query_rewritten": "...",
  "retrieval_mode": "hybrid",
  "top_k_sources": ["runbooks/weblogic-oom.md"],
  "faithfulness": 0.9,
  "relevance": 0.85,
  "latency_ms": 1240
}
```

复用 `log_kv()` 机制，不引入新依赖。

---

### Step 8：summarizer 展示评估结果

ops_qa 分支在答案后附加：
```
置信度：0.87 | 忠实度：0.90 | 相关性：0.85
```
仅当 `evaluation` 字段存在时展示（enable_eval=True 路径）。

---

### Step 9：测试补充

目标：59 → 80+ passed

新增：
- `test_knowledge_tool.py`：hybrid 路径、conversation_history 注入、qa_turns 写入 session
- `test_knowledge_evaluator.py`：faithfulness/relevance mock LLM 打分，EvalResult 字段正确
- 现有测试全部继续通过

---

## 实现顺序

```
Step 1（planning 占位清理）
→ Step 2（indexer chunk_index）
→ Step 3（hybrid + RRF）
→ Step 4（query rewriting）
→ Step 5（多轮上下文）
→ Step 6（evaluator）
→ Step 7（observability）
→ Step 8（summarizer）
→ Step 9（测试）
```

---

## 验收

```bash
# 全量测试
python -m pytest tests/ -q
# 期望：80+ passed, 0 failed

# 多轮对话
aiops-agent chat
# > WebLogic OOM 怎么处理  → 完整答案
# > 第二步怎么操作         → 理解指代，不重新解释第一步

# 混合检索（需 OPENAI_API_KEY + configs/rpa.json index_mode=hybrid）
aiops-agent knowledge index --force
aiops-agent knowledge query "JVM 堆内存不足排查"
# 日志：retrieval_mode=hybrid

# 评估（需 enable_eval=true）
aiops-agent knowledge eval "WebLogic OOM 如何处理"
# 输出：faithfulness=0.9+, relevance=0.85+
```

---

## 不做

- **流式输出**：`ToolExecutionResult` 是同步 dict，打通 tool→controller→chat 需改动执行链路，本轮不做
- **LangSmith / RAGAS**：评估逻辑自己实现
- **reranker 模型**：RRF 已够用
- **LangGraph 图结构修改**：多轮和评估都在 tool 层实现
- **新增数据库依赖**：qa_turns 存 session.metadata JSON 字符串

---

## 执行总结（20260510）

### 测试结果

| 阶段 | 通过数 |
|------|--------|
| 执行前 | 59 |
| 执行后 | **81** |
| 新增测试 | 22 |
| 失败 | 0 |

### 实际变更文件

**新增文件（1）**
- `src/aiops_agent/knowledge/evaluator.py`：`RAGEvaluator` + `EvalResult`，faithfulness / relevance LLM-as-judge 评分，`_score()` 对非数值输出 fallback 0.5，对越界分数 clamp 到 [0, 1]

**修改文件（10）**

`src/aiops_agent/config.py`
- `KnowledgeConfig` 新增字段 `enable_eval: bool = False`
- `load_rpa_config()` 从 JSON 读取 `enable_eval`

`src/aiops_agent/knowledge/indexer.py`
- `split_docs()` 按 source 分组写入递增的 `chunk_index`，确保 RRF 去重 key `source::chunk_index` 唯一

`src/aiops_agent/knowledge/retriever.py`
- 新增 `retrieve_hybrid(question, bm25, bm25_docs, vector_db)`：BM25 + vector 双路检索后送入 RRF
- 新增 `_rrf_merge(lists, k=60)`：RRF score = Σ 1/(k+rank)，按 `source::chunk_index` 去重，同时出现在两路的文档得分叠加排前
- 新增 `rewrite_query(question, history)`：history 为空直接返回原句（零延迟）；有历史时调 LLM 改写为独立问句，LLM 失败 fallback 原句
- 新增 `synthesize_with_history(question, docs, history)`：在 system prompt 后注入最近 5 轮 Q&A history block；`synthesize()` 委托给它（history=[]）

`src/aiops_agent/knowledge/engine.py`
- `query()` 新增 `conversation_history` 参数，串联：`rewrite_query` → 检索分支（keyword / vector / hybrid）→ `synthesize_with_history`
- `enable_eval=True` 时调用 `RAGEvaluator.evaluate()`，将 `EvalResult.confidence` 写回 `answer.confidence`，`faithfulness`/`relevance` 写入 `answer.evaluation`
- 新增 `_get_hybrid()`：懒加载同时持有 bm25 和 vector_db
- `rebuild_index()` 补充 hybrid 模式分支
- 每次 query 调用 `log_kv()` 记录结构化日志：`query_original`, `query_rewritten`, `retrieval_mode`, `top_k_sources`, `faithfulness`, `relevance`, `latency_ms`

`src/aiops_agent/tools/knowledge.py`
- `KnowledgeAnswer` 新增字段 `evaluation: dict | None = None`
- `execute()` 从 params 读取 `conversation_history`，透传给 `engine.query()`
- `_to_dict()` 输出包含 `evaluation` 字段

`src/aiops_agent/planning.py`
- 清理 ops_qa plan 的 MVP 占位文字（`steps` / `notes` / `success_criteria`）
- 从 `entities.get("conversation_history", [])` 读取历史，写入 `ToolCallSpec.params["conversation_history"]`

`src/aiops_agent/agent/controller.py`
- `_task_plan_node()`：`ops_qa` 意图时从 `session.metadata["qa_turns"]`（JSON string）解析最近 5 轮，注入 `task.entities["conversation_history"]`
- `_persist_audit_node()`：task 完成且 `intent == "ops_qa"` 且 `status == "success"` 时，提取 answer text 追加到 `session.metadata["qa_turns"]`，保留最近 5 轮

`src/aiops_agent/agent/summarizer.py`
- ops_qa 分支：当 `evaluation` 字段存在时，在答案末尾追加 `置信度：x | 忠实度：x | 相关性：x`

`tests/test_knowledge_tool.py`
- `TestVaultIndexer`：新增 `test_split_docs_writes_chunk_index`，验证 chunk_index 从 0 递增连续
- `TestKnowledgeRetriever`：新增 5 个测试 — RRF 去重、RRF 双路加分、rewrite_query 无历史返回原句、rewrite_query 有历史调 LLM、synthesize_with_history 注入 history block
- `TestKnowledgeTool`：新增 3 个测试 — conversation_history 透传、evaluation 字段在 response 中、query 调用签名验证
- 新增 `TestQATurns`：3 个测试 — qa_turns 成功写入、qa_turns 上限 5 轮、失败时不写入

`tests/test_knowledge_evaluator.py`（新增文件）
- `TestEvalResult`：3 个测试 — confidence 为均值、perfect、zero
- `TestRAGEvaluator`：8 个测试 — evaluate 返回正确类型和分值、faithfulness / relevance 独立分值、score clamp 上界/下界、非数值 fallback、LLM 异常 fallback、evaluate 仅用前 5 个 doc

### 关键设计决策记录

**qa_turns 写入位置改为 controller._persist_audit_node**，而非原计划的 ContextCompressor。原因：`ContextCompressor.compress()` 是 web/browser 专用，ops_qa result 结构（`answer.answer`）与其完全不同，混入会导致职责混乱。

**confidence 在 enable_eval=False 时保持原 hardcode（1.0/0.0）**，enable_eval=True 时由 `EvalResult.confidence = (faithfulness + relevance) / 2` 覆盖。这样不破坏现有逻辑，又为面试场景提供可量化的置信度来源。

**RRF k=60** 是工业界惯例默认值（来自原始 RRF 论文），对典型 top-5 检索结果提供足够的排名平滑效果，无需调参。

**rewrite_query fallback 设计**：LLM 调用失败或返回空字符串时返回原句，确保查询改写是纯增益，不引入单点故障。
