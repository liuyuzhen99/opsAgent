AIOps Agent MVP 设计文档 v0.1

一、项目定位

本项目实现一个面向企业运维场景的智能体（AIOps Agent）最小可行版本（MVP），用于验证以下核心能力：

- 自然语言驱动运维任务执行
- 多自动化工具（RPA / Playwright）的统一调度
- 执行结果的结构化分析与输出
- 高风险操作的人机协同控制

该系统不追求“完全自动化”，而强调：

可控、可审计、可扩展的运维智能执行框架

⸻

二、MVP 目标

核心目标

构建一个最小闭环：

用户输入 → 任务理解 → 工具执行 → 结果分析 → 输出反馈

支持能力范围

1. 巡检任务自动执行（基于 RPA）
2. 权限管理自动化（基于 Playwright）
3. 运维知识问答（Ops Copilot）
4. 人工确认机制（关键节点介入）
5. 执行日志与审计能力

⸻

三、系统总体架构

系统分为四层：

1. 接入层

- CLI输入
- 任务创建

2. Agent 核心层

- 意图识别
- 任务拆解
- 工具调度
- 结果总结

3. 工具层

- RPA 工具
- Playwright 工具
- 知识库工具
- 人工确认工具

4. 支撑层

- 权限控制
- 审计日志
- 任务状态管理

⸻

四、核心模块设计

4.1 Agent Controller

职责：

- 统一调度 Agent 执行流程
- 控制任务生命周期

核心方法：

run(task)

⸻

4.2 Intent Parser

职责：

- 将自然语言转换为结构化任务类型

支持类型：

- inspection
- permission_change
- ops_qa

输出示例：

{
"intent": "inspection",
"entities": {
"system": "WebLogic",
"env": "prod"
}
}

⸻

4.3 Task Manager

职责：

- 管理任务生命周期
- 持久化任务状态

状态流转：

pending → running → success / failed

⸻

4.4 Tool Registry

职责：

- 注册所有可用工具
- 提供统一调用入口

接口：

execute(tool_name, params)

⸻

4.5 工具模块

4.5.1 RPA 巡检工具

功能：

- 调用影刀执行巡检流程

输入：

- 巡检类型
- 系统名称

输出：

- 巡检结果
- 异常列表

⸻

4.5.2 Playwright 权限工具

功能：

- 自动化账号创建与权限分配

输入：

- 用户名
- 系统
- 角色

输出：

- 执行结果
- 操作日志

⸻

4.5.3 运维知识工具

功能：

- 查询 SOP 与运维知识

输入：

- 问题描述

输出：

- 标准操作步骤
- 注意事项

⸻

4.5.4 人工确认工具

功能：

- 在关键节点暂停执行

输出：

- confirmed / rejected

⸻

4.6 Result Summarizer

职责：

- 将工具结果转为可读报告

输出格式：

任务类型：
执行状态：
异常信息：
建议操作：

⸻

4.7 Permission Checker

职责：

- 控制高风险操作

策略示例：

- 权限变更必须人工确认
- 禁止执行敏感命令

⸻

4.8 Audit Logger

职责：

- 记录所有操作日志

记录内容：

- 用户输入
- 调用工具
- 执行结果
- 是否人工确认

⸻

五、核心执行流程

5.1 巡检流程

1. 用户输入巡检指令
2. 识别为 inspection
3. 调用 RPA 工具
4. 获取巡检结果
5. 生成总结报告

⸻

5.2 权限管理流程

1. 用户输入权限请求
2. 提取用户、系统、角色
3. 触发人工确认
4. 调用 Playwright 执行
5. 输出执行结果

⸻

5.3 知识问答流程

1. 用户提问
2. 调用知识库
3. 返回标准答案

⸻

六、数据模型设计

Task

{
id,
type,
input,
status,
result
}

ToolResult

{
success,
data,
error
}

⸻

七、目录结构

aiops-agent/
├── agent/
├── tools/
├── tasks/
├── permissions/
├── hooks/
├── knowledge/
├── runtimes/
├── storage/
└── configs/
⸻

九、验收标准

- 可执行巡检任务
- 可执行权限任务
- 可进行运维问答
- 支持人工确认
- 支持日志记录

⸻

十、非目标

- 多 Agent 协作
- 自主决策系统
- 复杂记忆学习
- 全自动运维平台

⸻

十一、总结

该 MVP 的核心价值在于：

- 打通“自然语言 → 运维执行”的链路
- 建立统一工具调度能力
- 引入可控的人机协同机制

为后续扩展 AIOps 平台奠定基础。
