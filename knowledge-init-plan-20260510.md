# opsAgent 知识库 (Obsidian + RAG) 实现计划 v2

> 基于原计划 agile-pondering-lecun.md，结合 review 意见修订。修订要点见每节末尾的 **[修订]** 标注。

## Context

opsAgent 已完成 Phase 2：LangGraph 编排内核 + Browser Agent。当前 `KnowledgeTool`（`tools/knowledge.py`）是纯占位实现。`planning.py` 已把 `ops_qa` 意图路由到 `knowledge` 工具，config 已有 `KnowledgeConfig`，LangChain 依赖已存在。本计划完成从"工具契约"到"真实可查询知识库"的跨越。

---

## Part 1：Obsidian Vault 配置

### 1.1 Vault 文件夹结构

```
ops-knowledge/
├── .obsidian/
├── runbooks/
│   ├── deploy-weblogic.md
│   └── rollback-procedure.md
├── incidents/
│   └── 2026-04-weblogic-oom.md
├── troubleshooting/
│   └── jvm-heap-full.md
├── architecture/
│   └── weblogic-cluster.md
├── monitoring/
│   └── alert-rules.md
├── templates/          ← agent 默认排除
└── archive/            ← agent 默认排除
```

**每篇文档 frontmatter 标准：**

```yaml
---
title: WebLogic OOM 故障处理手册
tags: [weblogic, jvm, oom, prod, runbook]
system: WebLogic
env: prod
severity: P1
last_updated: 2026-05-10
---
```

### 1.2 configs/rpa.json 示例（含 knowledge 节点）

**[修订 #7]** 原 `configs/rpa.json` 缺少 knowledge 节点；明确验收前必须手动添加，以下为完整示例：

```json
"knowledge": {
  "vault_path": "/path/to/ops-knowledge",
  "include_patterns": ["*.md"],
  "exclude_patterns": [".obsidian/**", "attachments/**", "archive/**", "templates/**"],
  "index_mode": "keyword",
  "embedding_provider": "openai",
  "embedding_api_key": "",
  "embedding_model": "text-embedding-3-small",
  "embedding_base_url": ""
}
```

`embedding_api_key` 留空时从环境变量 `OPENAI_API_KEY` 读取，与主 LLM provider 完全解耦。

---

## Part 2：Agent 实现

### 2.1 整体架构

```
KnowledgeTool.execute(question)
        │
        ▼
  KnowledgeEngine                 ← 新增
        ├── VaultIndexer          ← 新增
        │       ├── BM25（keyword 模式，内存构建，不序列化）
        │       └── Chroma DB（vector 模式，持久化）
        └── KnowledgeRetriever    ← 新增
                └── LangChain LCEL chain（max_tokens=2048）
```

### 2.2 新增依赖

```toml
# pyproject.toml 新增
"langchain-community>=0.3.0",
"chromadb>=0.6.0",
"rank-bm25>=0.2.2",
"PyYAML>=6.0",          # frontmatter 解析
```

嵌入模型（`index_mode = "vector"` 时）：

- **首选：OpenAI `text-embedding-3-small`**（需独立 `embedding_api_key`，见 2.3）
- **备选：本地 BAAI/bge-m3**（离线中文环境，需额外安装 `langchain-huggingface`）

### 2.3 KnowledgeConfig 扩展

**[修订 #1]** 原计划复用 `LLMProviderConfig.api_key` 做嵌入，但主 LLM provider 默认是 Anthropic，无法直接用于 OpenAI 嵌入 API。新增独立 embedding 配置字段：

```python
@dataclass(slots=True)
class KnowledgeConfig:
    vault_path: str = ""
    include_patterns: list[str] = field(default_factory=lambda: ["*.md"])
    exclude_patterns: list[str] = field(
        default_factory=lambda: [".obsidian/**", "attachments/**", "archive/**", "secrets/**"]
    )
    index_mode: str = "keyword"
    # embedding 独立配置，与主 LLM provider 解耦
    embedding_provider: str = "openai"           # openai | huggingface
    embedding_api_key: str = ""                  # 空时读 OPENAI_API_KEY 环境变量
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str = ""
```

`load_rpa_config()` 中新增对应读取逻辑，`embedding_api_key` 优先从 `OPENAI_API_KEY` 环境变量读取。

### 2.4 意图识别增强

#### 方案选择

采用**方案 D（LLM 分类 + 规则 fallback）**：

- LLM 可用时：`classify_intent()` → intent
- LLM 不可用时：规则 fallback

#### 具体改造

**`agent/parser.py`：**

**[修订 #3]** 原计划把 QA 优先级移到 web_action 之前，会导致"如何访问监控页面"等被误识别为 `ops_qa`。改为在 ops_qa 规则内加**反向过滤**，而不调整检查顺序：

```python
QA_KEYWORDS = (
    "怎么", "如何", "why", "what", "知识库", "sop",
    "是什么", "什么意思", "步骤", "手册", "runbook",
    "排查", "troubleshoot", "处理", "解决", "原因",
    "告警", "故障", "incident", "最佳实践",
)

# 规则顺序不变：inspection → web_action → permission → qa → chat
# ops_qa 规则内加反向过滤：
if (any(keyword in lowered for keyword in self.QA_KEYWORDS)
        and not any(keyword in lowered for keyword in self.WEB_ACTION_KEYWORDS)):
    return IntentResult(intent="ops_qa", entities={"raw_text": normalized})
```

**`llm/langchain_provider.py`：**

优化 `classify_intent` prompt 中 `ops_qa` 的语义描述：

```
ops_qa: 用户询问运维知识、操作步骤、故障排查、系统说明等，不需要执行实际操作
```

### 2.5 新增文件

```
src/aiops_agent/knowledge/
├── __init__.py
├── indexer.py      ← VaultIndexer
├── retriever.py    ← KnowledgeRetriever
└── engine.py       ← KnowledgeEngine
```

#### `knowledge/indexer.py`

**[修订 #4]** `DirectoryLoader` 不支持 exclude_patterns glob，改用 `pathlib.Path.glob` 手动枚举 + `fnmatch` 过滤：

```python
class VaultIndexer:
    CHUNK_SIZE = 800
    CHUNK_OVERLAP = 100

    def _iter_docs(self) -> list[Document]:
        # 用 pathlib 枚举 *.md，用 fnmatch 应用 exclude_patterns
        # 逐文件解析 frontmatter（PyYAML）→ Document.metadata

    def build_keyword(self) -> tuple[BM25Okapi, list[Document]]:
        # 构建 BM25 索引，返回 (bm25, docs) 不序列化到磁盘
        # [修订 #5] keyword 模式不序列化（BM25 重建 <1s，pickle 有安全隐患）

    def build_vector(self) -> Chroma:
        # OpenAIEmbeddings(model=..., api_key=...) 批量嵌入
        # Chroma.from_documents(persist_directory=vault/.chroma)

    def is_vector_stale(self) -> bool:
        # [修订 #6] 不用 mtime 对比目录，而是对比 index_manifest.json
        # manifest 记录建索引时所有文件的 {path: mtime} 映射
        # 有新增/修改/删除即为 stale
```

#### `knowledge/retriever.py`

**[修订 #2]** `synthesize()` 构建 LangChain chain 时显式指定 `max_tokens=2048`，不依赖全局 `LLMProviderConfig.max_tokens`（默认 512 会截断知识合成输出）：

```python
class KnowledgeRetriever:
    TOP_K = 5
    SYNTHESIS_MAX_TOKENS = 2048

    def retrieve_keyword(self, question: str, bm25: BM25Okapi, docs: list[Document]) -> list[Document]:
        # tokenize question → BM25 scores → top-K docs

    def retrieve_vector(self, question: str, db: Chroma) -> list[Document]:
        # db.similarity_search(question, k=TOP_K)

    def synthesize(self, question: str, docs: list[Document]) -> KnowledgeAnswer:
        # 显式 max_tokens=SYNTHESIS_MAX_TOKENS，覆盖全局配置
        # LangChain LCEL: prompt | llm | StrOutputParser
        # → KnowledgeAnswer(answer, sources)
```

**[建议 #9]** `synthesize()` 使用的模型通过 `role_models["knowledge"]` 可单独配置（复用已有机制），文档中应提及此配置路径。

#### `knowledge/engine.py`

```python
class KnowledgeEngine:
    def __init__(self, config: KnowledgeConfig, llm_config: LLMProviderConfig): ...

    def query(self, question: str) -> KnowledgeAnswer:
        # 懒加载：keyword 模式每次 query 时构建 BM25（<1s）
        #          vector 模式首次 query 时加载/重建 Chroma
        # 按 index_mode 分发
```

### 2.6 改造 `tools/knowledge.py`

```python
def execute(self, params: dict) -> ToolExecutionResult:
    question = str(params.get("question", ""))
    if not self.config.vault_path:
        # 保留原有占位逻辑
        ...
    answer = self.engine.query(question)
    return ToolExecutionResult(
        success=True,
        data={"question": question, "answer": self._to_dict(answer)}
    )
```

`KnowledgeTool.__init__` 接收 `llm_config` 参数，创建 `KnowledgeEngine`。

### 2.7 改造 `cli.py`

工厂函数中传入 `llm_config`：

```python
knowledge_tool = KnowledgeTool(config=rpa_config.knowledge, llm_config=anthropic_config)
```

新增子命令：

```bash
aiops-agent knowledge index           # 重建索引（stale 时）
aiops-agent knowledge index --force   # 强制重建
aiops-agent knowledge query "WebLogic OOM 怎么处理"  # 跳过 agent 编排直接查询
```

---

## Part 3：实现步骤（修订后 MVP 顺序）

**[修订 #8]** 原计划 Step 2 编号重复，重新编号如下：

### Step 1：更新 KnowledgeConfig + load_rpa_config()

- `config.py`：新增 `embedding_provider / embedding_api_key / embedding_model / embedding_base_url`
- `load_rpa_config()`：新增对应读取逻辑，`embedding_api_key` 从 `OPENAI_API_KEY` 环境变量回退

### Step 2：更新 pyproject.toml + configs/rpa.json

- 新增 `langchain-community`, `chromadb`, `rank-bm25`, `PyYAML`
- `configs/rpa.json` 补充完整 knowledge 节点示例

### Step 3：新建 knowledge 模块骨架

- 创建 `src/aiops_agent/knowledge/__init__.py`
- 创建空文件 `indexer.py / retriever.py / engine.py`

### Step 4：实现 VaultIndexer（keyword 模式）

- `pathlib.glob` + `fnmatch` 枚举 vault .md 文件（正确处理 exclude_patterns）
- `PyYAML` 解析 frontmatter → `Document.metadata`
- `RecursiveCharacterTextSplitter` 分块
- `BM25Okapi` 构建，返回 `(bm25, docs)`，不序列化

### Step 5：实现 KnowledgeRetriever（keyword + LLM 合成）

- BM25 检索 → top-K docs
- LangChain LCEL chain，显式 `max_tokens=2048`
- 返回 `KnowledgeAnswer(answer, sources)`

### Step 6：实现 KnowledgeEngine，改造 KnowledgeTool

- `KnowledgeEngine.query()` 懒加载
- 改写 `KnowledgeTool.execute()`
- `cli.py` 工厂函数传入 `llm_config`

### Step 7：增强 IntentParser + classify_intent prompt

- `QA_KEYWORDS` 扩展
- ops_qa 规则内加反向过滤（不调整检查顺序）
- `langchain_provider.py`：优化 ops_qa prompt 描述

### Step 8：新增 CLI knowledge 子命令

- `aiops-agent knowledge index [--force]`
- `aiops-agent knowledge query <question>`

### Step 9：实现 VaultIndexer（vector 模式）

- `OpenAIEmbeddings`（使用 `KnowledgeConfig.embedding_api_key`，独立于主 LLM）
- `Chroma` 持久化
- `is_vector_stale()` 基于 `index_manifest.json` 判断（不用 mtime 对比目录）
- `KnowledgeRetriever.retrieve_vector()`

### Step 10：测试

- `tests/test_knowledge_tool.py`（mock vault，验证检索+合成路径）
- `tests/test_knowledge_indexer.py`（临时目录，真实索引构建）
- `tests/test_intent_parser.py` 补充 ops_qa 路由用例（含反向过滤验证）

---

## 技术选型理由

| 决策               | 选择                          | 理由                                                     |
| ------------------ | ----------------------------- | -------------------------------------------------------- |
| 向量库             | ChromaDB                      | 本地持久化、无需外部服务                                 |
| keyword 检索       | rank-bm25（内存，不序列化）   | 零 API 依赖；vault < 500 篇时重建 <1s，pickle 有安全风险 |
| 嵌入模型           | OpenAI text-embedding-3-small | 独立 api_key，与主 LLM provider 解耦                     |
| 合成 max_tokens    | 显式 2048                     | 覆盖全局 512 默认，防截断                                |
| vault 文件枚举     | pathlib.glob + fnmatch        | DirectoryLoader 不支持 exclude glob                      |
| stale 检测         | index_manifest.json           | 目录 mtime 跨 FS 不可靠                                  |
| QA/web_action 分类 | 反向过滤（不调整优先级）      | 避免"如何访问页面"被误判为 ops_qa                        |

---

## 关键文件

| 文件                                        | 操作                                   |
| ------------------------------------------- | -------------------------------------- |
| `src/aiops_agent/config.py`                 | 更新 KnowledgeConfig + load_rpa_config |
| `src/aiops_agent/agent/parser.py`           | 扩展 QA_KEYWORDS + 反向过滤            |
| `src/aiops_agent/llm/langchain_provider.py` | 优化 ops_qa prompt                     |
| `src/aiops_agent/tools/knowledge.py`        | 改造（接入 KnowledgeEngine）           |
| `src/aiops_agent/knowledge/__init__.py`     | 新增                                   |
| `src/aiops_agent/knowledge/indexer.py`      | 新增                                   |
| `src/aiops_agent/knowledge/retriever.py`    | 新增                                   |
| `src/aiops_agent/knowledge/engine.py`       | 新增                                   |
| `src/aiops_agent/cli.py`                    | 工厂函数 + knowledge 子命令            |
| `pyproject.toml`                            | 新增依赖                               |
| `configs/rpa.json`                          | 补充 knowledge 节点                    |

---

## 验收方式

```bash
# 1. 配置 vault_path
# 编辑 configs/rpa.json，将 knowledge.vault_path 指向实际 vault 目录

# 2. 设置 OpenAI API key（vector 模式需要）
export OPENAI_API_KEY=sk-...

# 3. 建索引（keyword 模式）
aiops-agent knowledge index

# 4. 直接查询
aiops-agent knowledge query "WebLogic OOM 如何排查"

# 5. 通过 agent run 查询（验证 ops_qa 意图路由）
aiops-agent run "WebLogic 出现 OOM 我应该怎么处理"

# 6. 通过 chat 查询（验证反向过滤不误杀 web_action）
aiops-agent chat
# 输入："WebLogic OOM 如何排查"         → 应路由 ops_qa
# 输入："如何访问运维控制台"             → 应路由 web_action（反向过滤验证）
# 输入："部署回滚的步骤是什么"           → 应路由 ops_qa

# 7. 验证 sources 非空
# 8. 跑单元测试
python -m pytest tests/test_knowledge_tool.py tests/test_knowledge_indexer.py tests/test_intent_parser.py
```

---

## 实施记录（2026-05-10）

### 完成状态

全部 10 个步骤已实施完毕，59 个测试全部通过（含 12 个新增知识库测试 + 3 个新增意图路由测试），无回归。

### 变更文件清单

| 文件                                                                                   | 操作                                                                                                                                                                                                    | 对应 review 修订 |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| [src/aiops_agent/config.py](src/aiops_agent/config.py)                                 | `KnowledgeConfig` 新增 `embedding_provider / embedding_api_key / embedding_model / embedding_base_url`；`load_rpa_config` 从 `OPENAI_API_KEY` 环境变量回退                                              | #1               |
| [pyproject.toml](pyproject.toml)                                                       | 新增 `langchain-community>=0.3.0`, `chromadb>=0.6.0`, `rank-bm25>=0.2.2`, `PyYAML>=6.0`                                                                                                                 | #2               |
| [configs/rpa.json](configs/rpa.json)                                                   | 补充完整 knowledge 节点（含 embedding 字段示例）                                                                                                                                                        | #7               |
| [src/aiops_agent/knowledge/\_\_init\_\_.py](src/aiops_agent/knowledge/__init__.py)     | 新增模块入口                                                                                                                                                                                            | —                |
| [src/aiops_agent/knowledge/indexer.py](src/aiops_agent/knowledge/indexer.py)           | 新增 `VaultIndexer`：pathlib+fnmatch 枚举（替代 DirectoryLoader）、YAML frontmatter 解析、BM25 内存构建（不序列化）、Chroma vector 持久化 + `index_manifest.json` stale 检测                            | #4 #5 #6         |
| [src/aiops_agent/knowledge/retriever.py](src/aiops_agent/knowledge/retriever.py)       | 新增 `KnowledgeRetriever`：BM25/vector 检索 + LangChain LCEL 合成（显式 `max_tokens=2048`，支持 `role_models["knowledge"]`）                                                                            | #2 #9            |
| [src/aiops_agent/knowledge/engine.py](src/aiops_agent/knowledge/engine.py)             | 新增 `KnowledgeEngine`：懒加载，按 `index_mode` 分发到 keyword/vector                                                                                                                                   | —                |
| [src/aiops_agent/tools/knowledge.py](src/aiops_agent/tools/knowledge.py)               | 改造：接入 `KnowledgeEngine`；LLM 未启用时给出明确提示；接收独立 `llm_config` 参数                                                                                                                      | #1               |
| [src/aiops_agent/agent/parser.py](src/aiops_agent/agent/parser.py)                     | 扩展 `QA_KEYWORDS`（11 → 18 个词）；ops_qa 规则加反向过滤（不调整检查顺序）                                                                                                                             | #3               |
| [src/aiops_agent/llm/langchain_provider.py](src/aiops_agent/llm/langchain_provider.py) | `classify_intent` prompt 明确 5 个 intent 的语义定义，`ops_qa` 强调"不需要执行实际操作"                                                                                                                 | —                |
| [src/aiops_agent/cli.py](src/aiops_agent/cli.py)                                       | 工厂函数传 `llm_config` 给 `KnowledgeTool`；新增 `knowledge index [--force]` 和 `knowledge query <question>` 子命令                                                                                     | —                |
| [tests/test_knowledge_tool.py](tests/test_knowledge_tool.py)                           | 新增 12 个测试：`VaultIndexer`（枚举、frontmatter、exclude、BM25构建）、`KnowledgeRetriever`（BM25检索、空文档合成、LLM合成 mock）、`KnowledgeTool`（无 vault_path、路径不存在、LLM 禁用、engine 集成） | —                |
| [tests/test_intent_parser.py](tests/test_intent_parser.py)                             | 追加 3 个测试：`ops_qa` 关键词路由、反向过滤（含 web_action 关键词不路由到 ops_qa）、扩展关键词验证                                                                                                     | #3               |

### 关键实现决策记录

1. **VaultIndexer 不依赖 DirectoryLoader**：`DirectoryLoader` 无 exclude 参数，改用 `pathlib.glob("**/*.md")` + `fnmatch` 过滤，frontmatter 用 `yaml.safe_load` 手动解析，比 `ObsidianLoader` 更可控。

2. **BM25 不序列化**：keyword 模式每次 `query()` 时实时构建 BM25（典型 vault < 500 篇，构建 < 0.1s），避免 pickle 反序列化安全风险。

3. **vector stale 检测用 manifest**：在 `vault/.chroma/index_manifest.json` 存储 `{path: mtime}` 映射，`is_vector_stale()` 对比文件系统当前状态，比对比目录 mtime 更准确。

4. **embedding 与主 LLM 完全解耦**：`KnowledgeConfig.embedding_api_key` 优先从 `OPENAI_API_KEY` 环境变量读取，主 LLM 可继续使用 Anthropic，不互相干扰。

5. **合成 max_tokens 显式 2048**：`KnowledgeRetriever._build_synthesis_model()` 硬编码 `SYNTHESIS_MAX_TOKENS = 2048`，不受全局 `LLMProviderConfig.max_tokens=512` 影响。

6. **ops_qa 反向过滤**：规则检查顺序不变（inspection → web_action → permission → qa → chat），仅在 ops_qa 规则内加 `not any(keyword in lowered for keyword in WEB_ACTION_KEYWORDS)` 过滤，避免"如何访问监控页面"误判为 ops_qa。

### 下一步

1. 在 `configs/rpa.json` 中填入实际 `vault_path`
2. 在 vault 中放入运维文档（参考 Part 1 frontmatter 规范）
3. 执行 `aiops-agent knowledge index` 建立 BM25 索引
4. 通过 `aiops-agent knowledge query "WebLogic OOM 如何处理"` 验收检索效果
5. 如需 vector 模式：设置 `OPENAI_API_KEY`，修改 `index_mode` 为 `"vector"`，重新建索引

```

```
