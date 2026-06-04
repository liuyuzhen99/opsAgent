# Phase 1 Review Report

## 1. 背景与目标

本次工作分成两个连续阶段：

1. 先按现有 `phase1-plan.md` 实现一个最小可运行闭环。
2. 在你指出“缺少大模型 API 配置”后，把当前实现升级成“真正的 Phase 1”。

最终目标不是只把代码写出来，而是把以下链路真正跑通：

`CLI 输入 -> 自然语言理解 -> Agent 调度 -> 巡检工具执行 -> 结果总结输出`

## 2. 第一步：确认仓库现状和文档约束

### 做了什么

- 检查仓库根目录和文件结构。
- 搜索与 `Phase 1`、计划文档、实现边界相关的内容。
- 读取 `codex.md` 中的 MVP 描述、模块设计、里程碑和验收标准。

### 为什么这么做

- 在几乎空仓库的情况下，先确认“要实现什么”比直接写代码更重要。
- `codex.md` 是当时唯一的需求来源，必须先从文档中确认 Phase 1 的范围，避免把 Phase 2/3 的内容提前做进来。

### 怎么做的

- 用 `ls`、`find`、`rg`、`sed` 读取仓库内容。
- 重点确认了以下结论：
  - 仓库初始状态基本为空，只有 [codex.md](/Users/randy/Documents/code/opsAgent/codex.md:1)。
  - 文档把 Phase 1 定义为：
    - CLI 输入
    - Agent 基本循环
    - 巡检工具接入
  - 当时文档没有明确写出 LLM 配置和接入方式。

### 结果

- 明确了第一版实现的边界。
- 也为后续你提出“真正的 Phase 1 应该接大模型”埋下了调整依据。

## 3. 第二步：产出并落盘 Phase 1 计划

### 做了什么

- 基于 `codex.md` 先整理出一份可实施的 Phase 1 计划。
- 将计划写入 [phase1-plan.md](/Users/randy/Documents/code/opsAgent/phase1-plan.md:1)。

### 为什么这么做

- 原始文档只给了目标，没有把模块边界、输入输出接口和测试要求展开。
- 落一份明确计划可以让后续实现不至于边写边改方向，也方便你随时检查偏差。

### 怎么做的

- 把 Phase 1 拆成：
  - 项目骨架
  - CLI 入口
  - Agent 主流程
  - 巡检工具适配器
  - 任务模型
  - 输出格式
  - 测试范围
- 用 Markdown 形式整理成一个实施基线。

### 结果

- 有了 [phase1-plan.md](/Users/randy/Documents/code/opsAgent/phase1-plan.md:1) 作为第一轮开发依据。

## 4. 第三步：搭建 Python 项目骨架

### 做了什么

- 建立 Python 项目配置和包结构。
- 新增以下关键文件和目录：
  - [pyproject.toml](/Users/randy/Documents/code/opsAgent/pyproject.toml:1)
  - [aiops_agent/__main__.py](/Users/randy/Documents/code/opsAgent/aiops_agent/__main__.py:1)
  - [aiops_agent/cli.py](/Users/randy/Documents/code/opsAgent/aiops_agent/cli.py:1)
  - `agent/`
  - `tools/`
  - `tasks/`
  - `storage/`
  - `configs/`
  - 若干占位目录和 `.gitkeep`

### 为什么这么做

- 这是整个 MVP 的承载结构，没有这些目录，后面模块会散落在根目录里，后续 Phase 2/3 很难扩展。
- `pyproject.toml` 提供统一入口，方便用 `python3 -m aiops_agent` 或脚本命令执行。

### 怎么做的

- 在 [pyproject.toml](/Users/randy/Documents/code/opsAgent/pyproject.toml:1) 中声明包信息和 CLI script：
  - `aiops-agent = "aiops_agent.cli:main"`
- 用标准包结构组织模块，避免一开始就引入额外框架依赖。

### 结果

- 项目具备了最小可运行的 Python 包结构。

## 5. 第四步：实现第一版 Agent 主流程

### 做了什么

- 实现任务模型和任务状态管理。
- 实现 `AgentController.run(task_input)` 主流程。
- 实现结果总结器。

### 为什么这么做

- 文档里的核心闭环本质上就是一个串行控制流。
- 如果没有 `Task`、状态流转和报告生成，CLI 只能变成“执行一个函数”，无法体现 Agent 基本循环。

### 怎么做的

- 在 [aiops_agent/tasks/models.py](/Users/randy/Documents/code/opsAgent/aiops_agent/tasks/models.py:1) 中定义：
  - `Task`
  - `ToolResult`
- 在 [aiops_agent/tasks/manager.py](/Users/randy/Documents/code/opsAgent/aiops_agent/tasks/manager.py:1) 中实现：
  - `create_task`
  - `mark_running`
  - `mark_success`
  - `mark_failed`
  - `persist`
- 在 [aiops_agent/agent/controller.py](/Users/randy/Documents/code/opsAgent/aiops_agent/agent/controller.py:1) 中实现：
  - 接收输入
  - 调用解析器
  - 创建任务
  - 执行工具
  - 更新状态
  - 生成报告
- 在 [aiops_agent/agent/summarizer.py](/Users/randy/Documents/code/opsAgent/aiops_agent/agent/summarizer.py:1) 中输出固定格式：
  - 任务类型
  - 执行状态
  - 异常信息
  - 建议操作

### 结果

- 主流程具备了 Agent 雏形，而不是单纯脚本调用。

## 6. 第五步：实现第一版自然语言解析

### 做了什么

- 实现了第一版 `IntentParser`。
- 第一版只使用规则和关键词识别：
  - `inspection`
  - `permission_change`
  - `ops_qa`

### 为什么这么做

- 当时是按原始计划先打通最小闭环。
- 在没有明确 LLM 设计之前，规则解析能最快支撑巡检任务跑通。

### 怎么做的

- 在 [aiops_agent/agent/parser.py](/Users/randy/Documents/code/opsAgent/aiops_agent/agent/parser.py:1) 中：
  - 用关键词识别意图
  - 用简单规则抽取 `system`
  - 用正则抽取 `env`
  - 把原始文本放进 `raw_text`

### 结果

- 第一版已经能识别“巡检生产环境 WebLogic”这类输入。
- 但这一步也留下了明显局限：还不是真正的自然语言理解。

## 7. 第六步：实现巡检工具注册和真实适配器结构

### 做了什么

- 实现统一工具基类与注册中心。
- 实现巡检工具 `InspectionTool`。
- 新增 RPA 配置文件 [configs/rpa.json](/Users/randy/Documents/code/opsAgent/configs/rpa.json:1)。

### 为什么这么做

- 文档要求的是“工具统一调度”，不是把巡检逻辑直接塞进 Agent。
- 巡检工具需要具备真实接入能力，所以平台地址、流程映射和认证不能硬编码。

### 怎么做的

- 在 [aiops_agent/tools/base.py](/Users/randy/Documents/code/opsAgent/aiops_agent/tools/base.py:1) 定义工具基类和异常。
- 在 [aiops_agent/tools/registry.py](/Users/randy/Documents/code/opsAgent/aiops_agent/tools/registry.py:1) 实现注册与执行入口。
- 在 [aiops_agent/tools/inspection.py](/Users/randy/Documents/code/opsAgent/aiops_agent/tools/inspection.py:1) 中：
  - 校验配置完整性
  - 根据 provider 构造请求地址
  - 发起 HTTP 请求
  - 解析返回结果
  - 统一整理成 `ToolResult`
- 在 [aiops_agent/config.py](/Users/randy/Documents/code/opsAgent/aiops_agent/config.py:1) 中实现 RPA 配置加载。

### 结果

- 巡检工具具备了真实平台接入结构。
- 即便平台不可达、返回异常或配置缺失，也能返回结构化失败结果，而不会直接崩溃。

## 8. 第七步：实现 CLI 入口和任务持久化

### 做了什么

- 实现 CLI 命令入口。
- 实现本地任务持久化。

### 为什么这么做

- 文档里接入层明确包含 CLI。
- 任务持久化虽然是最小版本，但这是“任务生命周期管理”的最低要求。

### 怎么做的

- 在 [aiops_agent/cli.py](/Users/randy/Documents/code/opsAgent/aiops_agent/cli.py:1) 中定义：
  - `aiops-agent run "<task_input>"`
  - `--config`
- 在 [aiops_agent/storage/task_store.py](/Users/randy/Documents/code/opsAgent/aiops_agent/storage/task_store.py:1) 中把任务保存为 `storage/tasks/<task_id>.json`

### 结果

- 可以从命令行直接执行任务。
- 每次执行都有结构化任务记录落盘。

## 9. 第八步：补测试和第一轮验证

### 做了什么

- 新增单元测试和集成测试。
- 进行静态编译检查和手工路径验证。

### 为什么这么做

- Phase 1 虽然是 MVP，但如果没有至少一轮自动化测试和显式验证，后续升级时很容易把主链路改坏。

### 怎么做的

- 在 [tests/test_intent_parser.py](/Users/randy/Documents/code/opsAgent/tests/test_intent_parser.py:1) 中验证：
  - 巡检意图识别
  - 非巡检输入分类
- 在 [tests/test_agent_flow.py](/Users/randy/Documents/code/opsAgent/tests/test_agent_flow.py:1) 中验证：
  - 巡检成功路径
  - 配置缺失失败路径
- 执行了：
  - `python3 -m compileall aiops_agent tests`
  - 手工 CLI 调用

### 结果

- 第一版主链路确认可用。
- 同时也暴露出一个产品层面的缺口：没有接入大模型。

## 10. 第九步：根据反馈识别 Phase 1 的真实缺口

### 做了什么

- 根据你的反馈“为什么连大模型的 api 配置都没有”，重新审视当前实现是否满足“自然语言驱动运维任务执行”。

### 为什么这么做

- 这是一次需求澄清后的方向修正。
- 从产品语义上看，真正的 Phase 1 应该至少在“意图识别”这一层接入 LLM，否则更像规则原型或 PoC。

### 怎么做的

- 对照 [codex.md](/Users/randy/Documents/code/opsAgent/codex.md:1) 中“自然语言驱动运维任务执行”的目标重新评估。
- 得出结论：
  - 第一版实现是可运行闭环。
  - 但离“真正的 Phase 1”还差一层 LLM 解析能力。

### 结果

- 决定把当前代码升级为：
  - LLM 优先解析
  - 规则解析作为降级保底

## 11. 第十步：引入大模型配置与客户端

### 做了什么

- 新增 LLM 配置文件 [configs/llm.json](/Users/randy/Documents/code/opsAgent/configs/llm.json:1)。
- 新增 LLM 客户端 [aiops_agent/llm/client.py](/Users/randy/Documents/code/opsAgent/aiops_agent/llm/client.py:1)。
- 扩展配置加载逻辑。

### 为什么这么做

- 要让 Phase 1 真正具备“自然语言理解”能力，必须允许配置模型地址、密钥和模型名。
- 同时需要兼容真实 API 和本地/代理兼容接口。

### 怎么做的

- 在 `configs/llm.json` 中定义：
  - `enabled`
  - `provider`
  - `base_url`
  - `api_key`
  - `model`
  - `timeout_seconds`
- 在 [aiops_agent/config.py](/Users/randy/Documents/code/opsAgent/aiops_agent/config.py:1) 中新增 `load_llm_config`：
  - 支持配置文件
  - 支持环境变量覆盖：
    - `OPENAI_API_KEY`
    - `OPENAI_BASE_URL`
    - `OPENAI_MODEL`
    - 以及 `AIOPS_LLM_*`
- 在 [aiops_agent/llm/client.py](/Users/randy/Documents/code/opsAgent/aiops_agent/llm/client.py:1) 中：
  - 按 OpenAI 兼容 `chat/completions` 协议发请求
  - 明确 system prompt 和返回 JSON 约束
  - 校验返回内容
  - 统一抛出 `LLMError`

### 结果

- 项目第一次具备了真正可配置的模型能力接入口。

## 12. 第十一步：把 Intent Parser 升级为 LLM 优先

### 做了什么

- 将 `IntentParser` 从纯规则解析升级为：
  - 先走 LLM
  - 失败时自动回退到规则解析

### 为什么这么做

- 这样既满足“真正的 Phase 1”要求，又不会因为模型服务异常导致整个系统不可用。
- 对运维类系统来说，保底策略非常重要。

### 怎么做的

- 在 [aiops_agent/agent/parser.py](/Users/randy/Documents/code/opsAgent/aiops_agent/agent/parser.py:1) 中：
  - 注入 `LLMClient`
  - 新增 `_parse_with_llm`
  - 保留 `_parse_with_rules`
- LLM 成功时：
  - 输出 `intent`
  - 输出 `entities`
  - 自动补齐 `system`、`env` 默认值
  - 始终保留 `raw_text`
- LLM 失败时：
  - 捕获 `LLMError`
  - 进入规则解析

### 结果

- 意图识别从“关键词驱动”升级为“模型优先、规则兜底”。

## 13. 第十二步：升级 CLI 以支持 LLM 配置

### 做了什么

- 扩展 CLI 参数，支持传入 LLM 配置文件。

### 为什么这么做

- 如果只在代码里写死 LLM 配置，实际使用和切换环境会非常不方便。
- 运维场景通常会区分开发、测试、生产和代理网关，CLI 层必须允许显式指定配置。

### 怎么做的

- 在 [aiops_agent/cli.py](/Users/randy/Documents/code/opsAgent/aiops_agent/cli.py:1) 中新增：
  - `--llm-config`
- `create_controller()` 现在同时加载：
  - RPA 配置
  - LLM 配置
- 然后把 `LLMClient` 注入 `IntentParser`

### 结果

- CLI 现在不仅能跑巡检，也能控制是否启用和如何启用大模型解析。

## 14. 第十三步：扩展测试覆盖 LLM 场景

### 做了什么

- 补充了 LLM 相关测试。

### 为什么这么做

- 这是这次升级最重要的行为变化，如果不专门测，后续很容易出现：
  - 模型接入成功但结果格式不对
  - 模型失败时没有回退
  - CLI 没把配置真正传进去

### 怎么做的

- 在 [tests/test_intent_parser.py](/Users/randy/Documents/code/opsAgent/tests/test_intent_parser.py:1) 中新增：
  - LLM 成功解析测试
  - LLM 失败自动回退测试
- 在 [tests/test_agent_flow.py](/Users/randy/Documents/code/opsAgent/tests/test_agent_flow.py:1) 中新增：
  - Agent 通过 LLM 识别巡检任务，再调用巡检工具的集成测试

### 结果

- 新的关键行为被纳入了回归范围。

## 15. 第十四步：最终验证

### 做了什么

- 进行编译检查。
- 手工验证 LLM 成功路径和规则回退路径。

### 为什么这么做

- 当前环境没有安装 `pytest`，所以需要额外做一轮手工验证，避免“代码看起来没问题，但主链路根本没跑过”。

### 怎么做的

- 执行 `python3 -m compileall aiops_agent tests`
- 用本地 stub 模拟：
  - LLM 返回有效 JSON
  - 巡检工具返回成功结果
- 单独验证：
  - LLM 抛错时解析器仍能识别“巡检生产环境 WebLogic”

### 结果

- 编译检查通过。
- LLM 成功解析路径通过。
- LLM 失败回退路径通过。
- 由于本地没有 `pytest` 模块，未能执行 `python3 -m pytest -q`。

## 16. 最终交付物清单

### 新增或关键更新的文件

- [phase1-plan.md](/Users/randy/Documents/code/opsAgent/phase1-plan.md:1)
- [phase1-review-report.md](/Users/randy/Documents/code/opsAgent/phase1-review-report.md:1)
- [pyproject.toml](/Users/randy/Documents/code/opsAgent/pyproject.toml:1)
- [configs/rpa.json](/Users/randy/Documents/code/opsAgent/configs/rpa.json:1)
- [configs/llm.json](/Users/randy/Documents/code/opsAgent/configs/llm.json:1)
- [aiops_agent/cli.py](/Users/randy/Documents/code/opsAgent/aiops_agent/cli.py:1)
- [aiops_agent/config.py](/Users/randy/Documents/code/opsAgent/aiops_agent/config.py:1)
- [aiops_agent/agent/controller.py](/Users/randy/Documents/code/opsAgent/aiops_agent/agent/controller.py:1)
- [aiops_agent/agent/parser.py](/Users/randy/Documents/code/opsAgent/aiops_agent/agent/parser.py:1)
- [aiops_agent/agent/summarizer.py](/Users/randy/Documents/code/opsAgent/aiops_agent/agent/summarizer.py:1)
- [aiops_agent/llm/client.py](/Users/randy/Documents/code/opsAgent/aiops_agent/llm/client.py:1)
- [aiops_agent/tasks/models.py](/Users/randy/Documents/code/opsAgent/aiops_agent/tasks/models.py:1)
- [aiops_agent/tasks/manager.py](/Users/randy/Documents/code/opsAgent/aiops_agent/tasks/manager.py:1)
- [aiops_agent/storage/task_store.py](/Users/randy/Documents/code/opsAgent/aiops_agent/storage/task_store.py:1)
- [aiops_agent/tools/inspection.py](/Users/randy/Documents/code/opsAgent/aiops_agent/tools/inspection.py:1)
- [aiops_agent/tools/registry.py](/Users/randy/Documents/code/opsAgent/aiops_agent/tools/registry.py:1)
- [tests/test_intent_parser.py](/Users/randy/Documents/code/opsAgent/tests/test_intent_parser.py:1)
- [tests/test_agent_flow.py](/Users/randy/Documents/code/opsAgent/tests/test_agent_flow.py:1)

## 17. 总结

### 这次工作的核心变化

- 从零搭建了 AIOps Agent 的 Phase 1 基础结构。
- 先实现了一个可运行闭环。
- 再把它升级成真正具备 LLM 自然语言理解能力的 Phase 1。

### 当前实现的定位

- 已经不是单纯 PoC。
- 已具备：
  - CLI 接入
  - 任务生命周期
  - LLM 优先解析
  - 规则兜底
  - 巡检工具统一调度
  - 结果总结
  - 基础测试

### 还没有做的内容

- 权限变更实际执行链路
- 知识问答能力
- 人工确认
- 审计日志
- 真正的测试框架执行环境安装

### 一句话结论

这次 Phase 1 的实现路径是：先把系统“搭起来、跑起来”，再根据产品语义把“自然语言理解”补齐，最终把项目从规则式闭环升级成了带 LLM 解析能力的真正 Phase 1。
