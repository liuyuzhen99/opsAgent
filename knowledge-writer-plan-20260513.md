# Knowledge Writer 落地计划 20260513

## 背景

目标是在 `aiops-agent chat` 和 CLI 中支持显式知识沉淀：用户说“记录到知识库 / 保存到知识库 / 沉淀文档 / 写入 vault”，或在 chat 中使用 `/save-note` 时，agent 将当前输入与最近 QA 上下文整理成 Obsidian 主题笔记，写入 `knowledge.vault_path`，并更新对应分类 MOC。

本计划基于当前代码 review 调整原方案，重点修正以下风险：

- 显式写入不能只依赖 LLM intent prompt，否则 LLM 先行分类会绕过规则触发词。
- 审计不能记录完整笔记正文、用户说明或 QA 历史。
- `KnowledgeTool` 和 writer 必须共享同一个 `KnowledgeEngine`，否则写入后 chat 查询可能仍使用旧 cache。
- vector/hybrid 重建必须避免旧向量残留或重复。
- `qa_turns` 需要同时服务 `ops_qa` 与 `knowledge_write`。
- 成功摘要、CLI、chat 命令和测试都要补齐公共契约。

## 目标行为

### 触发方式

1. 自然语言显式触发：
   - 记录到知识库
   - 保存到知识库
   - 沉淀文档
   - 写入 vault
2. Chat 命令：
   - `/save-note [instruction]`
   - 无说明时使用最近 QA 上下文。
3. CLI：
   - `aiops-agent knowledge write "<instruction>" --dry-run`
   - 无 `--dry-run` 时真实写入。

### 写入结果

- 只写入 `knowledge.vault_path` 内，且不写入 exclude 目录。
- LLM 未启用时不写入，返回 `llm.enabled` 缺失信息。
- 新笔记类型目录：
  - `incident/`
  - `runbooks/`
  - `architecture/`
  - `guidance/`
- 未知类型默认 `runbooks`。
- 文件名：`{system} - {短标题}.md`。
- 同名文件存在时不覆盖，返回明确提示要求用户显式更新某笔记。
- frontmatter 固定字段：
  - `title`
  - `aliases`
  - `system`
  - `type`
  - `env`
  - `severity`
  - `tags`
  - `last_updated`
- 正文末尾维护 `# 相关知识`，仅写入可解析 `[[...]]` forward wikilinks。
- 更新目标目录下 `* MOC.md`，追加或更新：
  - `- [[笔记名]]：一句话说明`
- v1 不批量改写相关笔记，依赖 Obsidian backlinks 自动产生反链。

## 实施步骤

### Step 1：配置契约

在 `KnowledgeConfig` 新增：

- `write_enabled: bool = True`
- `auto_reindex_after_write: bool = True`
- `note_type_dirs: dict[str, str]`

`load_rpa_config()` 读取这些配置，并保持默认值。

### Step 2：确定性 intent 路由

在 `IntentParser.parse()` 中增加 LLM 前置硬路由：

- 如果命中知识写入触发词，直接返回 `knowledge_write`。
- 不让 LLM 把显式写入误判为 `ops_qa` 或 `general_chat`。

同时更新 `LangChainLLMProvider.SUPPORTED_INTENTS` 和 prompt，使 LLM 在非规则入口也理解 `knowledge_write`。

### Step 3：共享 KnowledgeEngine

调整 `KnowledgeTool` 支持注入现成 `KnowledgeEngine`。

`create_controller()` 中只创建一个 engine，并注入：

- `KnowledgeTool`
- `KnowledgeWriteTool`

`KnowledgeEngine` 增加：

- `invalidate_cache()`
- `reindex_after_write()`

vector/hybrid 重建时清理旧 Chroma 目录或 collection，避免残留旧向量。

### Step 4：KnowledgeNoteWriter

新增 writer 模块，负责：

- 校验 vault 路径、exclude 规则、写入开关和 LLM 开关。
- 从 instruction + 最近 `qa_turns` 生成结构化草稿。
- 规范化 type/system/env/severity/tags/aliases/title。
- 生成安全文件名并防止 path traversal。
- 生成 Markdown 与 frontmatter。
- 更新分类 MOC，避免重复 wikilink。
- dry-run 时返回目标路径和预览元数据，但不写文件、不更新 MOC、不重建索引。

LLM 输出使用 JSON schema，并做防御性 fallback：

- title 为空则从 instruction 生成短标题。
- type 非法则落到 `runbooks`。
- links 只保留简单 wikilink target，不接受嵌入、路径跳转或 markdown 链接。

### Step 5：Tool / Planner / Controller

新增 `knowledge_writer` tool，并在 `PlanningService` 中新增 `knowledge_write` plan。

`controller._task_plan_node()` 对 `knowledge_write` 同样注入最近 `qa_turns`。

审计 redaction：

- `task_created` 对 `knowledge_write` 不记录完整 input。
- `intent_parsed` 对 `knowledge_write` 不记录完整 entities。
- 可新增 `knowledge_write.completed` 审计事件，只记录 title/path/type/MOC/session/task id/reindex_status。

### Step 6：Chat / CLI / Summary

Chat：

- `/save-note [instruction]` 转为统一 controller 输入。
- 无 instruction 时传入“把上一条问答记录到知识库”。

CLI：

- `knowledge write "<instruction>" --dry-run`
- dry-run 走 writer 直接调用，真实写入可走 controller 或 tool；为保持统一审计，优先走 controller。

Summarizer：

- 对 `knowledge_write` 返回 note_path、title、type、MOC 更新状态、索引更新状态。
- LLM/vault/write_disabled/collision 错误显示为可读提示。

## 测试计划

- Parser：显式写入语句路由到 `knowledge_write`，普通知识问答仍为 `ops_qa`。
- LLM intent：支持 `knowledge_write`。
- Writer：fake LLM 生成草稿后创建合法 Markdown、frontmatter、相关知识链接和分类 MOC 行。
- Collision：同名笔记存在时不覆盖。
- MOC：重复执行不产生重复 wikilink。
- Chat：`/save-note` 复用当前 session 最近 QA。
- Controller：`knowledge_write` 走 planner、policy、tool、summarizer、audit 全链路。
- Index：写入成功后共享 engine cache 被刷新，hybrid/vector 模式触发重建。
- 审计：不记录完整正文、instruction、qa_turns。
- 回归：`python -m pytest tests/ -q`。

## 非目标

- v1 不批量更新其他已有笔记。
- v1 不实现“更新某笔记”的增量编辑。
- v1 不保证自动推断出的所有 wikilinks 一定存在，只保证语法可解析且不越界。

## 已实施变更总结

### Intent 与路由

- 新增 `knowledge_write` intent，并在 LLM 前增加确定性规则路由。
- 扩展触发词，覆盖“记录到知识库、保存到知识库、添加入知识库、添加到知识库、加入知识库、写入知识库、录入知识库、沉淀文档、写入 vault、整理成知识库、生成 knowledge”等表达。
- `knowledge_write` 不再默认套用 `WebLogic` 系统；只有输入中明确出现相关系统时才推断系统。
- LLM intent prompt 同步加入 `knowledge_write`，避免非规则入口下被归类为 `ops_qa`、`web_action` 或 `general_chat`。

### Writer 与工具链

- 新增 `KnowledgeNoteWriter` 和 `knowledge_writer` tool。
- writer 负责校验 `knowledge.vault_path`、exclude 目录、写入开关、LLM 开关、文件名安全和同名文件冲突。
- 支持 `incident/`、`runbooks/`、`architecture/`、`guidance/` 类型目录，未知类型默认写入 `runbooks`。
- 生成固定 frontmatter 字段：`title`、`aliases`、`system`、`type`、`env`、`severity`、`tags`、`last_updated`。
- 正文末尾维护 `# 相关知识`，只保留合法 `[[...]]` wikilinks。
- 写入成功后更新目标目录下的 `* MOC.md`，并避免重复 wikilink。
- 同名笔记存在时不覆盖，返回明确 collision 错误，要求用户显式说明更新目标笔记。

### 标签与内容校验

- 标签改为层级格式：
  - `system/财司系统`
  - `type/runbook`、`type/incident`、`type/architecture`、`type/guidance`
  - `component/weblogic`、`component/nginx`、`component/icip`、`component/ukey`、`component/堡垒机`
  - `env/prod`
  - `severity/P1`、`severity/P2`
- writer 会忽略 LLM 生成的非规范自由标签，统一做 canonicalize。
- component 标签必须能从实际笔记内容中匹配到，避免内容与 WebLogic 无关却生成 `component/weblogic`。
- 系统推断增加“财司 / 财司系统”识别；若 LLM 误给 `WebLogic` 但正文无 WebLogic 语义，会降级为 `unknown`。
- 对“请将以下内容添加入知识库”“将以下内容写入知识库：”这类没有正文的输入增加拦截，返回 `knowledge_write.content` 缺失，不再生成空笔记。

### Chat 与 CLI

- Chat 新增 `/save-note [instruction]`，走统一 controller/tool/audit 流程。
- `/save-note` 无参数时复用上一条用户输入，方便用户先粘贴内容，再保存为知识笔记。
- Chat 新增 `/note` 或 `/paste` 多行录入块，用户粘贴多行后用 `/end` 结束，避免 `input()` 一行一任务导致误触发。
- 可选启用 `prompt_toolkit`，改善中文删除、光标显示和多行输入体验；默认尝试支持 `Esc + Enter` 换行，并在终端支持时绑定 `Shift + Enter`。
- CLI 新增 `aiops-agent knowledge write "<instruction>" --dry-run`，无 `--dry-run` 时真实写入 vault。

### 索引、一致性与审计

- `KnowledgeTool` 与 `KnowledgeWriteTool` 共享同一个 `KnowledgeEngine`。
- `KnowledgeEngine` 新增 `invalidate_cache()` 和 `reindex_after_write()`。
- 写入成功后刷新 BM25/vector cache；`hybrid/vector` 模式按配置触发重建。
- vector 重建前清理旧 Chroma 存储，避免旧向量残留或重复命中。
- 审计事件对 `knowledge_write` 做脱敏：不记录完整正文、instruction、qa_turns，只记录 title、path、type、MOC、session/task id、reindex_status 等安全摘要。
- `ResultSummarizer` 对 `knowledge_write` 返回写入路径、类型、MOC 更新状态和索引状态。

### 测试覆盖

- Parser：覆盖显式知识写入触发词、`生成 knowledge`、`添加入知识库`、不默认 `WebLogic`、财司系统推断。
- Writer：覆盖 Markdown/frontmatter/MOC 生成、同名冲突、MOC 去重、LLM 未启用、层级标签规范化。
- Empty content：覆盖“请将以下内容添加入知识库”和“将以下内容写入知识库：”无正文时拒绝写入。
- Chat：覆盖 `/save-note`、无参数复用上一条输入、`/note ... /end` 多行块。
- Controller：覆盖 `knowledge_write` 从 planner、policy、tool、summarizer 到 audit 的完整链路。
- 回归测试结果：`python3 -m pytest tests/ -q` 通过，结果为 `135 passed, 6 skipped, 1 warning`。

## 后续优化建议

- 输入体验：继续验证不同 macOS 终端对 `Shift + Enter` 的键码支持；如果终端无法区分 Shift+Enter，可在启动提示中更明确推荐 `Esc + Enter` 和 `/note ... /end`。
- 粘贴体验：增加“检测到知识写入触发词但正文为空”后的交互式继续输入，引导用户直接粘贴正文，而不是只返回错误。
- 更新已有笔记：实现“更新某笔记 / 追加到某笔记”的显式路径，支持安全 diff、用户确认和 MOC 保持一致。
- 标签体系：把系统、组件、环境、严重级别做成可配置字典，避免代码中写死 `财司系统`、`weblogic`、`ukey` 等领域词。
- 主题分类：增加更稳的 note type 推断规则，例如接口资料默认 `guidance`、部署步骤默认 `runbooks`、故障复盘默认 `incident`。
- LLM 输出质量：加入更严格的 JSON schema 校验和重试，减少 title、summary、links、tags 漂移。
- 隐私保护：对 URL、IP、账号、密钥样式字段增加可选脱敏策略，并允许用户配置哪些字段原样入库。
- 索引性能：写入后优先做增量索引，只有 vector store 不支持或 schema 变化时再全量重建，减少首次写入时的 Hugging Face 模型加载成本。
- 离线模式：在 embedding 模型未下载或网络不可用时，允许先写入笔记和 MOC，并把 reindex 标记为 pending。
- 观测性：为写入失败原因增加结构化错误码，例如 `vault_not_configured`、`empty_content`、`collision`、`llm_disabled`、`reindex_failed`。
- 回滚能力：写入笔记成功但 MOC 或索引失败时，记录可恢复状态，后续可通过 CLI 执行 `knowledge repair`。
- 文档：补充面向用户的 chat 使用说明，分别说明“直接一句话写入”“先输入内容再 `/save-note`”“多行 `/note` 块”的推荐用法。

## 2026-05-14 修复记录

- 修复 SQL / Shell / 配置代码块可能被 LLM 摘要化后丢失的问题：writer prompt 明确要求保留原始命令，同时代码层会扫描用户输入中的 fenced code block；如果 LLM 生成的正文没有包含这些代码块，会自动在正文末尾追加 `## 原始资料` 并原样写入。
- 修复 `# 相关知识` 自动生成不存在 wikilink 的问题：`related_links` 不再只做语法过滤，而是必须能在 vault 中解析到真实存在的 Markdown 笔记文件；不存在的笔记名、非法链接、路径跳转、嵌入链接、heading/alias 链接都会被过滤。
- `# 相关知识` 不再链接 `* MOC.md` 文件；分类 MOC 的链接由 writer 单独维护，正文相关知识只保留具体知识笔记。
- 新增回归用例：LLM 草稿未包含 SQL 时仍会保留原始 fenced SQL；不存在的相关笔记不会进入正文双链；MOC 不会被作为正文相关知识链接。

## 2026-05-14 查询回答质量修复

- 修复长 runbook 被切块后回答不完整的问题：检索命中 chunk 后，合成答案前会按源文件读取完整 Markdown 正文，避免只拿到 SQL 的 SELECT 字段段或 ORDER BY 片段。
- 检索阶段改为先取更多候选，再优先保留具体知识笔记；当存在 runbook / architecture / incident 等具体文档时，README 和 `* MOC.md` 这类目录页会被过滤掉，不再挤占回答上下文。
- 合成 prompt 明确要求：文档包含 SQL、Shell、配置或代码块时必须完整保留相关代码块，不能只摘要字段或排序片段。
- 合成上下文 token 上限从 2048 提高到 4096，降低“对公 + 对私”两个 SQL 同时回答时被截断的概率。
- 新增回归用例：只命中 SQL 尾部 chunk 时，合成 prompt 仍包含完整源笔记 SQL；README/MOC 不会替代具体付款表 runbook 进入答案上下文。

## 2026-05-14 原始资料去重修复

- 修复 writer 在 LLM 正文已经包含 SQL 时仍追加 `## 原始资料` 的问题：从“字符串完全包含”改为“fenced code block 等价判断”，忽略缩进、换行、注释、大小写和少量尾部符号差异。
- 入库前会清理冗余 `## 原始资料` section：如果原始资料中的代码块已经在处理步骤等正文位置出现，则删除重复的原始资料 section。
- 当确实缺失用户输入中的代码块时，仍会追加 `## 原始资料` 兜底，保证 SQL / Shell / 配置不会丢失。
- 新增回归用例：LLM 同时在处理步骤和原始资料中放入等价 SQL、用户原始 SQL 尾部带多余 `]` 时，最终笔记只保留一份 SQL，不生成 `## 原始资料`。

## 2026-05-14 原始资料默认关闭

- 调整 writer prompt：明确要求不要默认生成 `## 原始资料`，只有 SQL / Shell / 配置或代码块无法融入正文且否则会丢失时才保留必要 fenced code block。
- 后处理规则改为：如果去掉 `## 原始资料` 后正文已经是结构化内容，删除原始资料中的普通原文；只保留正文里确实没有出现过的代码块。
- 如果正文已经在“处理步骤”等位置包含代码块，也视为已正常解析，不再额外生成 `## 原始资料`。
- 新增回归用例：LLM 生成结构化正文后又追加普通原文 `## 原始资料` 时，最终笔记会删除该 section。

## 2026-05-14 Chroma 自动重建锁处理

- 修复 chat 长驻进程中自动重建 hybrid/vector 索引间歇性失败的问题：强制重建前会显式关闭旧 Chroma client、释放 chromadb 进程级 system cache，并触发 GC，避免旧 SQLite 句柄继续占用 `.chroma`。
- `reindex_after_write()` 在 hybrid/vector 模式下重建成功后会释放刚创建的 Chroma 连接，只保留持久化索引文件；下一次查询再懒加载向量库，减少写入后长时间持有 SQLite 句柄。
- 对 `readonly database`、`unable to open database file`、`database is locked` 等 Chroma/SQLite 锁类错误增加短暂重试，仍然保持 hybrid 索引构建，不降级为 keyword。
- hybrid/vector 查询遇到旧连接失效时，会关闭旧连接并重新加载 `.chroma` 后重试一次完整向量检索，避免外部手动 `knowledge index --force` 后 chat 内旧连接导致查询失败。
- 新增回归用例：自动重建遇到 readonly database 后重试成功；hybrid 查询遇到 unable to open database file 后释放旧连接并重新打开向量库。
- 修正 Chroma 1.5.9 下过度清理 global system cache 引发 `KeyError('/path/.chroma')` 的问题：只调用 `client.close()` 释放引用计数，不再手动 `clear_system_cache()` 或 `system.stop()` 干预 Chroma 内部状态。
