# Phase 1 计划：CLI + Agent 基本循环 + 真实巡检工具接入

## Summary
基于 `codex.md` 的里程碑定义，Phase 1 目标是先打通最小可运行闭环：

`CLI 输入 -> 意图识别 -> Agent 调度 -> 巡检工具执行 -> 结果总结输出`

实现栈默认采用 Python，CLI 采用单次命令模式，巡检工具按真实 RPA 接入规划，而不是只做 mock。

## Key Changes
- 建立最小目录骨架，优先落地 `agent/`、`tools/`、`tasks/`、`storage/`、`configs/`。
- 实现 `AgentController.run(task_input)` 主流程，串起输入解析、任务创建、工具执行、结果总结和状态更新。
- Phase 1 仅支持 `inspection` 一种可执行意图；`permission_change` 和 `ops_qa` 保留接口与未实现提示。
- 巡检工具采用真实适配器结构，平台地址、流程标识和认证信息放入配置，不写死在代码中。
- 任务模型按最小集实现：`Task { id, type, input, status, result }` 和 `ToolResult { success, data, error }`。
- 输出报告遵循固定格式：任务类型、执行状态、异常信息、建议操作。

## Public APIs / Interfaces
- CLI 入口：`aiops-agent run "<自然语言任务>"`
- `AgentController.run(task_input: str) -> Task`
- `IntentParser.parse(text: str) -> IntentResult`
- `ToolRegistry.execute(tool_name: str, params: dict) -> ToolResult`
- `InspectionTool.execute(params: dict) -> ToolResult`
- `ResultSummarizer.summarize(task: Task, tool_result: ToolResult) -> str`

## Test Plan
- CLI 输入巡检指令，例如“巡检生产环境 WebLogic”，能够完成一次完整执行并输出总结。
- 非巡检类输入会被识别为未支持任务，返回清晰提示，不触发工具调用。
- 真实巡检工具成功时，`Task.status = success` 且输出包含巡检结果和建议操作。
- 真实巡检工具失败时，`Task.status = failed` 且输出包含结构化异常信息。
- 配置缺失、平台不可达、返回空结果等异常路径都要覆盖。
- 至少补一组 `IntentParser` 单元测试和一组 Agent 主流程集成测试。

## Assumptions
- Phase 1 不实现人工确认、权限控制、知识问答、日志审计；这些留到后续阶段。
- Python 为默认实现语言。
- CLI 只支持单次命令调用，不做 REPL。
- 巡检工具按真实 RPA 平台接入设计，但开发环境允许通过测试桩完成验证。
