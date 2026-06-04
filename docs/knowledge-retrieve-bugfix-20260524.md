# Knowledge 检索 Bugfix 说明

日期：2026-05-24

## 背景

用户在查询 knowledge 时输入：

```text
财司系统怎么发版
```

系统没有召回实际存在于 vault 中的知识笔记：

```text
/Users/randy/Desktop/yili/ops_knowledge/ops_knowledge/runbooks/财司系统 - 生产环境发版.md
```

而是召回了 `CBS`、`Ukey 信息`、`服务器自动运行脚本`、`系统信息` 等相关性较弱的文档，导致回答认为“财司系统生产环境发版”文档不存在或没有具体步骤。

## 问题复现

在修复前，使用现有 `configs/rpa.json` 指向的 vault 构建 keyword 索引并查询：

```bash
.venv/bin/python -c "from aiops_agent.config import load_rpa_config, load_anthropic_config; from aiops_agent.knowledge.indexer import VaultIndexer; from aiops_agent.knowledge.retriever import KnowledgeRetriever; cfg=load_rpa_config('configs/rpa.json').knowledge; llm=load_anthropic_config('configs/llm.json'); idx=VaultIndexer(cfg); bm25,docs=idx.build_keyword(); ret=KnowledgeRetriever(cfg,llm); q='财司系统怎么发版'; print('tokens', idx._tokenize(q)); res=ret.retrieve_keyword(q,bm25,docs); print([d.metadata.get('rel_path') for d in res])"
```

修复前输出中的 query tokens 类似：

```text
['财司系统怎么发版']
```

这意味着中文短句被当成一个完整 token，无法和文档中的 `财司系统`、`发版`、`生产环境发版` 等词片段匹配。

## 根因

### 1. 中文短句分词过粗

原实现使用：

```python
re.findall(r"\w+", text.lower())
```

在 Python 正则里，中文会被 `\w+` 识别为连续词块。因此：

```text
财司系统怎么发版
```

会被切成一个整体 token：

```text
财司系统怎么发版
```

BM25 依赖 token overlap 计算相关性。当 vault 文档中出现的是 `财司系统生产环境发版`、`财司发版`、`发版补丁包` 这类表达时，query 的整体 token 无法匹配，相关文档得分为 0 或很低。

### 2. 标题和路径没有充分进入 keyword 索引

Obsidian vault 中很多关键语义在文件名、title、alias、MOC wikilink 中，例如：

```text
财司系统 - 生产环境发版.md
title: 财司系统生产环境发版
aliases:
  - 财司发版
```

此前索引上下文已经包含 aliases、tags、属性、wikilink，但没有显式加入 title 和 rel_path/stem。对于“怎么发版”这类短查询，标题和文件名往往是最强召回信号，缺失后会放大召回偏差。

## 修复方案

### 1. 新增 knowledge tokenizer

新增文件：

```text
src/aiops_agent/knowledge/tokenizer.py
```

核心逻辑：

- ASCII/数字/下划线按连续 token 保留。
- 中文连续片段生成 2-gram 和 3-gram。
- 较短中文片段保留完整 token。
- 过长中文片段不保留完整 token，避免索引噪声过大。

修复后：

```text
财司系统怎么发版
```

会包含如下可匹配 token：

```text
财司
系统
发版
财司系
司系统
怎么发
么发版
```

### 2. indexer 和 retriever 使用同一套分词

修改：

```text
src/aiops_agent/knowledge/indexer.py
src/aiops_agent/knowledge/retriever.py
```

`VaultIndexer._tokenize()` 和 `KnowledgeRetriever.retrieve_keyword()` 都改为使用 `tokenize_knowledge_text()`，确保索引侧和查询侧 token 规则一致。

### 3. 将 title 和路径信息加入可检索上下文

在 `VaultIndexer._append_link_context()` 中追加：

```text
相关标题：...
相关路径：rel/path.md note_stem
```

这样 title、文件路径、文件名 stem 都可以参与 BM25 和向量索引。

### 4. 升级 vector manifest schema

`VaultIndexer.MANIFEST_SCHEMA_VERSION` 从 `2` 升级到 `3`。

原因是索引输入发生变化：title/path 上下文和 tokenizer 都会影响索引结果。升级 schema 后，hybrid/vector 模式会自动判定旧 `.chroma` 索引 stale，并触发重建，避免继续使用旧索引。

## 回归测试

修改：

```text
tests/test_knowledge_tool.py
```

新增覆盖：

1. `tokenize_knowledge_text("财司系统怎么发版")` 必须包含 `财司`、`系统`、`发版`。
2. 构造一个包含 20 个无关文档和 1 个目标文档的小 vault，验证 query `财司系统怎么发版` 的 keyword 召回第一名是：

```text
runbooks/财司系统 - 生产环境发版.md
```

3. 断言 Obsidian link context 中包含新增的 title 和 path 上下文。

## 验证结果

### knowledge 单测

```bash
.venv/bin/python -m pytest tests/test_knowledge_tool.py -q
```

结果：

```text
46 passed
```

### 全量测试

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
161 passed, 6 skipped
```

### 真实 vault 验证

修复后，用真实 vault 查询 keyword 召回：

```text
财司系统怎么发版
```

召回结果第一名为：

```text
runbooks/财司系统 - 生产环境发版.md
```

输出示例：

```text
[
  'runbooks/财司系统 - 生产环境发版.md',
  'runbooks/财司系统 - weblogic部署手册.md',
  'runbooks/财司系统 - 生产环境发版.md',
  'runbooks/财司系统 - 问题排查流程.md',
  'guidance/财司系统 - 财司老系统交易数据查询SQL.md'
]
```

## 影响范围

受影响模块：

```text
src/aiops_agent/knowledge/tokenizer.py
src/aiops_agent/knowledge/indexer.py
src/aiops_agent/knowledge/retriever.py
tests/test_knowledge_tool.py
src/aiops_agent.egg-info/SOURCES.txt
```

行为变化：

- 中文短句 keyword 检索更稳定。
- title、文件名、路径会参与检索。
- hybrid/vector 模式下旧索引会因 manifest schema v3 自动 stale，需要重建。

## 后续注意

1. 当前 tokenizer 是轻量 n-gram 方案，不依赖外部分词库，适合本项目本地可运行和离线场景。
2. 如果后续 vault 规模明显增大，可以考虑加入停用词、字段权重或 BM25 文档级去重。
3. 当前 hybrid 结果仍可能出现同一 source 的不同 chunk 重复，后续可以在 RRF 或 `_prefer_concrete_docs()` 后增加 source-level 去重。
4. 运行中的 chat/agent 进程需要重启后才能加载本次代码改动。
