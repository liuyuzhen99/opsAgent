# Knowledge / Obsidian 兼容性调整计划（2026-05-12）

## 目标

让 Knowledge 功能先以低风险方式兼容 Obsidian vault 的核心结构：真实 YAML Properties、aliases、中文业务属性、基础 wikilink、可检索的链接上下文，以及可重建的索引 manifest。图谱扩展、doctor CLI 和大规模 vault 自动整理作为后续阶段处理。

## 用户旅程

- 作为运维用户，我想用俗称或别名提问，例如“付款状态未知”，以便命中正式标题为“支付指令状态未知”的知识笔记。
- 作为知识库维护者，我想继续使用 Obsidian Properties 和双链，以便人可浏览的 vault 同时能被 Knowledge 检索理解。
- 作为开发者，我想在索引逻辑变更后自动判定向量索引过期，以便不会误用旧 Chroma 索引。

## 阶段一：本次执行范围

1. 补测试先行：覆盖 aliases、中文 Properties、基础 wikilink、link context、include patterns、manifest schema 变化、source relation 默认值。
2. 配置兼容：扩展 `KnowledgeConfig`，并同步更新 `load_rpa_config()` 字段映射。
3. 索引兼容：
   - 让 `include_patterns` 真正生效。
   - 解析真实 YAML frontmatter 中的 `aliases`、`type/类型`、`system/系统`、`env/环境`、`severity/严重度`、`component/组件`、`last_updated`。
   - 识别 fenced frontmatter 但不当作有效契约，只记录 metadata 标记，便于后续提示修正。
   - 解析基础 Obsidian wikilink：`[[目标]]`、`[[目标|显示文本]]`，识别 `![[附件]]` 但不作为普通知识链接。
   - 将 aliases、tags、业务属性、出链文本追加为机器可检索上下文。
4. Manifest 升级：从纯 `{path: mtime}` 改为带 `schema_version/files/index_options` 的结构；旧 manifest 自动 stale。
5. Source 兼容：给 `KnowledgeSource` 增加默认 `relation="direct"` 和 `related_to=""`，不破坏现有调用。
6. 验证：运行 targeted tests 和全量测试。

## 明确不在本次范围

- 不批量修改外部 Obsidian vault 源文件。
- 不实现 backlinks/MOC 的 1 跳图谱扩展。
- 不新增 `aiops-agent knowledge doctor` CLI。
- 不引入 Neo4j 或其他图数据库。

## 后续阶段建议

1. 先对 vault 做只读审计，列出 fenced frontmatter、broken wikilink、孤立笔记、缺失 aliases 的笔记。
2. 经用户确认后，再批量修正外部 vault。
3. 在有阶段一检索基线后，再实现 backlinks、MOC 识别、graph expansion 和 relation 排序权重。

## 阶段一实施总结（2026-05-12）

### 已完成事项

1. 生成并落地了本计划文件
   - 文件：`knowledge-obsidian-compatibility-plan-20260512.md`
   - 计划按 review 结果收敛为阶段一 MVP，避免一次性引入 backlinks、MOC 图谱扩展、doctor CLI 和外部 vault 批量改写。

2. 扩展 Knowledge 配置
   - `src/aiops_agent/config.py` 的 `KnowledgeConfig` 新增：
     - `obsidian_graph_enabled: bool = True`
     - `link_context_enabled: bool = True`
     - `graph_expand_depth: int = 1`
     - `graph_boost: float = 0.15`
     - `moc_patterns: list[str] = ["*MOC.md", "**/README.md"]`
   - `load_rpa_config()` 已同步读取这些字段，避免出现 dataclass 有字段但 `configs/rpa.json` 配置不生效的问题。

3. 修复 `include_patterns` 未生效的问题
   - `VaultIndexer.iter_docs()` 现在会同时检查 include 与 exclude。
   - manifest 文件列表也使用同一套 include/exclude 规则，避免索引内容和 stale 判断不一致。

4. 增强真实 YAML frontmatter 解析
   - 支持字段：
     - `title`
     - `tags`
     - `aliases`
     - `type` / `类型`
     - `system` / `系统`
     - `env` / `环境`
     - `severity` / `严重度`
     - `component` / `组件`
     - `last_updated`
   - 中文 Properties 会被归一化到内部英文 metadata key，例如 `系统` -> `system`、`类型` -> `type`。
   - list/dict 类型 metadata 会被扁平化为字符串，以降低 Chroma metadata 兼容风险。

5. 标记 fenced frontmatter，但不把它当作有效契约
   - 对形如 ```yaml 包裹的 frontmatter 增加 `has_fenced_frontmatter = True` metadata。
   - 不解析 fenced frontmatter 内的 title/tags 等字段，保持“真实 YAML frontmatter 才是 Obsidian 和代码共享契约”的原则。

6. 增加基础 Obsidian wikilink 解析
   - 支持：
     - `[[目标]]`
     - `[[目标|显示文本]]`
   - 对 `![[附件]]` 只识别为 embed，不作为普通知识链接写入 `outlinks_text`。
   - 当前阶段只生成 `outlinks_text`，还没有做 backlinks 和图谱扩展检索。

7. 将 Obsidian metadata 转成可检索上下文
   - 当 `link_context_enabled=True` 时，索引正文末尾会追加：
     - `相关别名：...`
     - `相关标签：...`
     - `相关属性：...`
     - `相关链接：...`
   - 这样 aliases、tags、中文属性和 wikilink 不只存在于 metadata，也能参与 BM25 和向量检索。

8. 升级向量索引 manifest
   - manifest 从旧的纯 `{path: mtime}` 结构升级为：
     - `schema_version`
     - `files`
     - `index_options`
   - 旧格式 manifest 会自动判定为 stale。
   - 当 `link_context_enabled`、`obsidian_graph_enabled`、`moc_patterns` 等索引相关配置变化时，也会触发 stale。

9. 扩展 Knowledge source 输出
   - `KnowledgeSource` 新增：
     - `relation: str = "direct"`
     - `related_to: str = ""`
   - 当前阶段所有 source 仍默认为 `direct`，为后续 graph expansion 的 `outlink/backlink/moc` 来源解释预留兼容字段。

10. 补充 TDD 测试
    - 新增覆盖：
      - 配置加载 Obsidian 兼容字段
      - `include_patterns` 生效
      - aliases / 中文 Properties / wikilink 解析
      - fenced frontmatter 只标记不解析
      - manifest schema v2 与旧 manifest stale
      - `KnowledgeSource.relation` 默认值

### 验证结果

已运行：

```bash
python -m pytest tests/test_knowledge_tool.py -q
```

结果：

```text
28 passed
```

已运行：

```bash
python -m pytest tests/ -q
```

结果：

```text
89 passed, 1 warning
```

warning 来自依赖包 `langgraph/cache/base` 的 pending deprecation warning，不是本次改动引入的功能失败。

### 当前未做事项

以下内容刻意没有在阶段一实现：

1. 没有批量修改外部 Obsidian vault
   - 原因：外部 vault 是知识源数据，不在当前仓库内，批量改 frontmatter 和链接属于高影响操作。
   - 后续应先做只读审计，再由用户确认是否自动修正。

2. 没有实现 backlinks
   - 当前只解析每篇文档的出链 `outlinks_text`。
   - backlinks 需要先建立文档级链接图，并处理同名笔记、路径解析、标题别名冲突等问题。

3. 没有实现 MOC 图谱扩展检索
   - 当前仅通过 `moc_patterns` 标记 `is_moc`。
   - 尚未在 retrieval 阶段把 MOC 或一跳邻居加入候选结果。

4. 没有实现 source relation 的 graph 值
   - 当前 `relation` 默认是 `direct`。
   - `outlink/backlink/moc` 需要等 graph expansion 实现后再赋值。

5. 没有新增 `aiops-agent knowledge doctor`
   - doctor CLI 是有价值的持续维护工具，但会引入新的 CLI 行为和测试矩阵，适合单独作为下一阶段。

## 后续待办

### 优先级 P0：用真实 vault 验证阶段一收益

1. 重建索引

```bash
aiops-agent knowledge index --force
```

2. 用真实问题验证 alias 和 link context 是否提升召回

建议查询：

```bash
aiops-agent knowledge query "支付状态未知怎么处理"
aiops-agent knowledge query "付款状态未知要查哪里"
aiops-agent knowledge query "财司系统打不开先看什么"
```

3. 对比验证点
   - “付款状态未知”是否能命中“支付指令状态未知”笔记。
   - 来源片段中是否包含追加的 `相关别名`、`相关属性` 或 `相关链接` 上下文。
   - hybrid 模式下 BM25 和向量结果是否仍稳定。

### 优先级 P1：只读 vault 审计

建议新增一个只读审计脚本或临时命令，先输出报告，不直接改文件。检查项：

1. fenced frontmatter 文件列表
2. 缺少真实 YAML frontmatter 的业务笔记
3. 缺少 `aliases` 的 incident/runbook
4. 缺少 `tags`、`系统/类型/组件/环境` 的笔记
5. broken wikilink
6. 孤立笔记：无出链且无反链
7. 未纳入 MOC/README 的业务笔记
8. 同名笔记或可能造成 wikilink 歧义的文件

输出建议包括：

- 文件路径
- 问题类型
- 严重程度
- 建议修复方式

### 优先级 P1：经确认后整理外部 vault

在用户确认后，可以分批修正 `/Users/randy/Desktop/yili/ops_knowledge/ops_knowledge`：

1. 把 fenced frontmatter 转为真实 YAML frontmatter。
2. 为 incident/runbook 补充 aliases。
3. 统一层级标签，例如：
   - `system/财司系统`
   - `type/incident`
   - `component/icip`
   - `env/prod`
   - `severity/P2`
4. 增加或完善 MOC：
   - 财司系统 MOC
   - ICIP MOC
   - WebLogic MOC
   - UKey MOC
5. 在具体笔记末尾补充 `## 相关知识` 双链。

安全要求：

- 修改前检查外部 vault 是否是 git repo。
- 如果是 git repo，先查看 status。
- 如果不是 git repo，先生成备份或 patch，避免不可逆批量改写。

### 优先级 P2：实现文档级 Obsidian graph index

建议新增轻量 graph 结构，而不是引入数据库：

```python
@dataclass
class ObsidianGraph:
    by_source: dict[str, GraphNode]
    backlinks: dict[str, list[str]]
```

GraphNode 可包含：

- `source`
- `rel_path`
- `title`
- `aliases_text`
- `outlinks`
- `backlinks`
- `is_moc`

需要处理：

1. `[[Note]]`
2. `[[Note|Alias]]`
3. `[[Folder/Note]]`
4. `[[Note#Heading]]`
5. 同名文件冲突
6. 文件名、title、alias 的解析优先级

### 优先级 P2：实现一跳 graph expansion

建议流程：

```text
query -> keyword/vector -> RRF direct chunks -> doc-level graph neighbors -> select neighbor chunks -> weighted merge -> synthesize
```

实现建议：

1. direct 命中仍保留最高权重。
2. graph 邻居只扩展 1 跳。
3. 每个邻居文档只加入最相关的 1-2 个 chunk。
4. MOC 节点要限流，避免中心节点污染结果。
5. 新增配置：
   - `initial_top_k`
   - `graph_neighbor_limit`
   - `final_top_k`
6. graph 来源设置：
   - `relation="outlink"`
   - `relation="backlink"`
   - `relation="moc"`
   - `related_to="原始命中文档标题或路径"`

### 优先级 P2：补 source relation 展示和调试信息

后续 graph expansion 完成后，应在回答来源中展示为什么引用某篇文档：

- direct：直接语义/关键词命中
- outlink：由直接命中文档的出链引入
- backlink：由反链引入
- moc：由 MOC 主题节点引入

这能帮助调试检索质量，也能让用户理解答案为什么引用某篇相关文档。

### 优先级 P3：新增 `aiops-agent knowledge doctor`

doctor CLI 建议作为独立功能实现，输出 vault 质量报告。

建议命令：

```bash
aiops-agent knowledge doctor
aiops-agent knowledge doctor --json
```

建议检查项：

1. fenced frontmatter
2. broken wikilink
3. orphan notes
4. missing aliases
5. missing required properties
6. duplicate titles / ambiguous wikilinks
7. MOC coverage
8. stale index manifest

### 优先级 P3：增加真实评估集

建议为运维知识库维护一个小型查询评估集，例如：

```json
[
  {
    "query": "付款状态未知要查哪里",
    "expected_sources": ["财司系统 - 支付指令状态未知"]
  },
  {
    "query": "财司系统打不开先看什么",
    "expected_sources": ["财司系统 - 问题排查流程"]
  }
]
```

用途：

- 对比阶段一前后召回变化。
- 后续 graph expansion 调参时避免引入噪声。
- CI 中可做轻量 smoke test。

## 当前工作区备注

- `.claude/settings.local.json` 有权限 allowlist 变更，按用户要求保留。
- 测试运行产生的 pycache 改动已清理。
- 当前阶段没有修改外部 Obsidian vault 源文件。
