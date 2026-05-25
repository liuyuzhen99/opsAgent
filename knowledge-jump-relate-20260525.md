# Knowledge 跳转检索与自动关联改造说明

日期：2026-05-25

## 背景

Knowledge 功能在使用 Obsidian vault 时暴露出两个相关问题：

1. 查询问题时，即使召回文档中存在指向目标知识的 wikilink，系统也不会继续读取链接目标正文。
2. 写入新笔记时，文末 `# 相关知识` 主要依赖 LLM 返回的 `related_links`，无法可靠地从 vault 中自动找到已有相关内容。

典型场景是查询：

```text
财司系统怎么发版
```

此前可能先召回：

```text
runbooks/财司系统 - weblogic部署手册.md
```

该文档的相关知识区域实际包含：

```markdown
- [[财司系统 - 生产环境发版]]：财司系统发版步骤
```

并且链接对应文档确实存在：

```text
runbooks/财司系统 - 生产环境发版.md
```

但原查询链路只把 wikilink 文本送给 LLM，没有解析并加载目标 Markdown 正文，最终可能错误回答“对应文档不存在”。

## 目标

本次改造包含两个目标：

1. 查询时支持根据直接命中文档的一跳 wikilink 加载真实目标文档，并让相关目标正文优先进入回答上下文。
2. 写入时自动在现有 vault 中检索相关具体笔记，并补充到新笔记文末的 `# 相关知识` 中。

## 查询链路改造

### 1. 保存可解析的 wikilink 目标

修改文件：

```text
src/aiops_agent/knowledge/indexer.py
```

此前索引元数据只保存用于搜索的文本：

```text
outlinks_text
```

现在额外保存仅包含真实跳转目标的：

```text
outlink_targets
```

例如内容：

```markdown
- [[财司系统 - 服务的启停|服务启停]]
- [[财司系统 - 生产环境发版]]
```

会解析为：

```text
财司系统 - 服务的启停
财司系统 - 生产环境发版
```

显示别名不会被误当成要解析的目标，附件嵌入 `![[...]]` 也不会参与文档跳转。

### 2. 根据 wikilink 解析 vault 中的真实笔记

`VaultIndexer` 新增出链展开能力：

```python
expand_outlinks(docs: list[Document]) -> list[Document]
```

实现行为：

- 扫描 vault 中现有 Markdown 笔记。
- 通过相对路径、文件名 stem 和 frontmatter `title` 建立文档查找表。
- 解析直接召回文档的 `outlink_targets`。
- 将确实存在的目标 Markdown 文档加载为候选上下文。
- 使用 `seen_sources` 避免重复展开和环路。
- 按 `graph_expand_depth` 控制跳转层数；当前默认配置支持一跳展开。

展开后的文档增加来源元数据：

```text
relation = "outlink"
related_to = "直接命中文档的标题"
```

### 3. 将出链文档合并进合成上下文

修改文件：

```text
src/aiops_agent/knowledge/engine.py
src/aiops_agent/knowledge/retriever.py
```

查询流程现在为：

```text
query
  -> keyword / vector / hybrid 直接召回
  -> 展开直接命中文档中的有效 wikilink
  -> 按当前问题与目标标题/别名/路径的匹配程度筛选出链文档
  -> 将相关出链文档置于合成上下文前部
  -> LLM 回答
```

`KnowledgeSource` 输出会保留来源关系。例如当部署手册引入发版文档时，来源信息可以表达为：

```text
section: runbooks/财司系统 - 生产环境发版.md
relation: outlink
related_to: WebLogic安装手册
```

### 4. 更新索引版本

`VaultIndexer.MANIFEST_SCHEMA_VERSION` 升级到：

```text
4
```

原因是索引元数据新增 `outlink_targets`，旧 vector/hybrid 索引不具备该链接解析信息。升级后已有 `.chroma` 索引会被判定为 stale，从而在后续重建时生成新的索引内容。

## 写入链路改造

### 1. 从 vault 自动发现已有相关笔记

修改文件：

```text
src/aiops_agent/knowledge/writer.py
```

`KnowledgeNoteWriter.write()` 在 LLM 生成并规范化草稿后，现在会调用：

```python
_discover_related_links(...)
```

使用以下信息构造检索文本：

- 新笔记的 system
- title
- summary
- aliases
- body
- 用户原始写入 instruction
- 最近三轮 QA 上下文

然后针对当前 vault 构建的 BM25 索引检索已有笔记。

### 2. 自动关联排序规则

自动发现不是简单采用 LLM 猜测，而是对真实存在的文档排序：

- 使用 knowledge tokenizer 处理中文及中英文混合文本。
- 使用 BM25 作为正文相关性基础分。
- 对标题、别名和相对路径与草稿主题的词重叠进行加权。
- 对正文词重叠提供小幅补充分。
- 对搜索 token 去重，防止草稿中重复表达的通用词放大排名。
- 只保留达到最高分相对阈值的候选。
- 最多自动加入 5 条相关笔记链接。

当前参数：

```python
AUTO_RELATED_LINK_LIMIT = 5
AUTO_RELATED_SCORE_RATIO = 0.4
```

### 3. 自动结果与 LLM 结果合并

写入时最终相关链接由两部分组成：

```text
LLM related_links + vault 自动检索 related_links
```

合并后继续走原有的合法性校验逻辑，确保：

- 链接必须对应真实存在的 Markdown 笔记。
- 不写入当前新建笔记自身。
- 不写入 `README.md`。
- 不写入 `* MOC.md`。
- 不接受嵌入链接、非法路径、外部 URL 或不合法 wikilink 表达。
- 自动去重。

即使 LLM 返回：

```json
{"related_links": []}
```

writer 也能够通过 vault 检索补入相关内容。

### 4. 生成笔记效果

例如写入一篇“财司系统生产环境发版补充检查”的笔记，如果 vault 中已有服务启停知识，生成笔记末尾可以自动包含：

```markdown
# 相关知识

- [[财司系统 - 服务的启停]]
```

## 修改文件

本次功能改造涉及：

```text
src/aiops_agent/knowledge/indexer.py
src/aiops_agent/knowledge/engine.py
src/aiops_agent/knowledge/retriever.py
src/aiops_agent/knowledge/writer.py
tests/test_knowledge_tool.py
```

其中：

- `indexer.py`：解析真实 wikilink 目标、实现出链展开、升级 manifest schema。
- `engine.py`：在查询合成前接入 graph/outlink 上下文展开。
- `retriever.py`：按问题相关性合并出链文档，并输出来源关系。
- `writer.py`：写入时检索 vault 并自动补充相关知识链接。
- `test_knowledge_tool.py`：增加查询跳转和写入自动关联回归测试。

## 测试覆盖

### 查询跳转测试

新增用例覆盖：

1. wikilink 解析后保存真实 `outlink_targets`，不将 alias 或 embed 误作为目标。
2. `weblogic部署手册` 链接到存在的 `财司系统 - 生产环境发版.md` 时，indexer 可以解析并标记：

```text
relation = outlink
```

3. 即使直接检索结果仅包含 `weblogic部署手册`，查询“财司系统怎么发版”时，LLM 合成上下文仍会获得目标发版文档中的实际步骤。
4. 答案来源中发版文档会标记为由部署手册引入的 outlink 来源。

### 写入自动关联测试

新增用例覆盖：

1. LLM 草稿明确返回空的 `related_links`。
2. vault 中已有 `财司系统 - 服务的启停.md`。
3. 写入财司系统生产发版相关笔记后，writer 自动生成：

```markdown
- [[财司系统 - 服务的启停]]
```

4. `Runbooks MOC.md` 不会被误写入正文相关知识。

## 验证结果

### Knowledge 测试

```bash
.venv/bin/python -m pytest tests/test_knowledge_tool.py -q
```

结果：

```text
49 passed
```

### 全量测试

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
170 passed, 6 skipped
```

测试期间存在两个既有依赖警告：

- LangGraph 序列化配置将来的默认值变更警告。
- 测试调用的 `claude-sonnet-4-20250514` 模型将在 2026-06-15 到期的弃用警告。

这两个警告与本次功能实现无直接关系。

### 真实 vault 查询跳转验证

从真实 vault 的：

```text
runbooks/财司系统 - weblogic部署手册.md
```

成功解析到以下存在的 outlink 文档：

```text
runbooks/财司系统 - 服务的启停.md
runbooks/财司系统 - 生产环境发版.md
```

针对问题：

```text
财司系统怎么发版
```

即便从部署手册作为直接入口，合并后的上下文排序也会将：

```text
runbooks/财司系统 - 生产环境发版.md
```

作为 `outlink` 来源优先提供给 LLM。

### 真实 vault 自动关联验证

以“财司系统生产环境发版时需要核对服务启停顺序”为草稿主题执行只读自动关联检索，可发现以下已有笔记：

```text
财司系统 - 生产环境发版
财司系统 - weblogic部署手册
财司系统 - 系统信息
财司系统 - 服务的启停
```

## 运行注意事项

1. 运行中的 chat 或 agent 进程需要重启，才能加载新的查询跳转与写入自动关联逻辑。
2. hybrid/vector 模式的旧索引需要按照 manifest schema v4 刷新；后续重建或 stale 检测会触发更新。
3. 自动关联当前采用轻量 BM25 与标题加权方案，优先保证无需额外外部模型、可离线运行。
4. 若 vault 进一步扩大，可以继续加入字段权重配置、候选类型过滤、去通用系统词策略或反向链接推荐。
