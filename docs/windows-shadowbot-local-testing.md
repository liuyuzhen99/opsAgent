# Windows 本地接入影刀免费版测试说明

## 1. 目标

本文档说明如何在 Windows 环境下，把当前项目接入影刀免费版（ShadowBot）进行本地巡检测试。

当前项目已经支持两种 RPA 执行模式：

- `api`
  适用于企业版或具备平台接口的远程调用方式
- `shadowbot_local`
  适用于 Windows 本地安装的影刀免费版，通过本地命令直接启动流程

如果你使用的是影刀免费版，请使用 `shadowbot_local`。

## 2. 当前支持的接入方式

项目中的巡检工具位于 [src/aiops_agent/tools/inspection.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/tools/inspection.py:1)。

在 `shadowbot_local` 模式下，系统会在 Windows 上调用类似下面的命令：

```bat
cmd /c start "" "D:\Program Files\ShadowBot\ShadowBot.exe" "shadowbot:Run?robot-uuid=YOUR-ROBOT-UUID"
```

这和你调研到的影刀免费版启动方式一致。

## 3. 前置条件

在 Windows 上测试前，需要先满足以下条件：

1. 已在 Windows 机器上安装影刀免费版。
2. 能手动通过影刀打开并运行目标机器人。
3. 已确认目标机器人的 `robot-uuid`。
4. 已把本项目代码放到可运行 Python 的 Windows 环境中。
5. 已安装项目依赖。

建议先验证下面这条命令在 Windows 上能手动拉起影刀：

```bat
start "" "D:\Program Files\ShadowBot\ShadowBot.exe" "shadowbot:Run?robot-uuid=YOUR-ROBOT-UUID"
```

如果这条命令不能运行，项目侧也无法正常触发本地 RPA。

## 4. 配置方式

Windows 本地测试主要依赖 [configs/rpa.json](/Users/randy/Documents/code/opsAgent/configs/rpa.json:1)。

示例配置如下：

```json
{
  "provider": "yidao",
  "execution_mode": "shadowbot_local",
  "platform_url": "",
  "timeout_seconds": 10,
  "auth": {
    "type": "bearer",
    "token": ""
  },
  "inspection": {
    "default_system": "WebLogic",
    "default_env": "prod",
    "flow_map": {
      "WebLogic": "YOUR-ROBOT-UUID"
    }
  },
  "shadowbot": {
    "executable_path": "D:\\Program Files\\ShadowBot\\ShadowBot.exe",
    "robot_uuid": "",
    "command_timeout_seconds": 10,
    "result_file": ""
  }
}
```

### 字段说明

- `execution_mode`
  必须设置为 `shadowbot_local`
- `inspection.flow_map`
  系统名到影刀机器人 UUID 的映射
- `shadowbot.executable_path`
  影刀可执行文件路径
- `shadowbot.robot_uuid`
  可选
  如果留空，系统会自动使用 `inspection.flow_map` 中对应系统的 UUID
- `shadowbot.command_timeout_seconds`
  启动命令的超时时间
- `shadowbot.result_file`
  可选
  如果后续影刀流程能把结果输出成 JSON 文件，可以在这里填写路径，系统会尝试读取

## 5. 推荐配置方式

对于影刀免费版，推荐这样配置：

```json
{
  "inspection": {
    "flow_map": {
      "WebLogic": "你的影刀机器人UUID"
    }
  },
  "shadowbot": {
    "executable_path": "D:\\Program Files\\ShadowBot\\ShadowBot.exe",
    "robot_uuid": ""
  }
}
```

这样每个系统都可以映射到不同的机器人，不需要写死一个全局 UUID。

## 6. LLM 配置说明

如果你只是先验证影刀本地联调，可以暂时不开启 LLM。

配置文件 [configs/llm.json](/Users/randy/Documents/code/opsAgent/configs/llm.json:1) 中保持：

```json
{
  "enabled": false
}
```

这样系统会自动使用规则解析，例如：

- `巡检生产环境 WebLogic`
- `检查生产 WebLogic`

都可以触发 `inspection`。

## 7. 运行步骤

### 方式一：使用已安装的 CLI

```bash
aiops-agent run --config configs/rpa.json --llm-config configs/llm.json "巡检生产环境 WebLogic"
```

### 方式二：直接用 Python 模块运行

```bash
python -m aiops_agent run --config configs/rpa.json --llm-config configs/llm.json "巡检生产环境 WebLogic"
```

## 8. 预期结果

如果本地命令成功拉起影刀，项目会返回类似结果：

```text
任务类型：inspection
执行状态：success
异常信息：无
建议操作：巡检通过，无需额外处理。
```

在当前实现里，这里的成功更准确地说是：

- 成功触发了本地 ShadowBot 启动命令

默认情况下，如果没有配置 `result_file`，系统会把本次结果标记为：

- `inspection_result = launched`

这意味着：

- 已经成功启动影刀流程
- 但还没有拿到影刀执行完成后的真实业务结果

## 9. 日志与审计

当前项目已经支持统一日志、trace id 和审计事件。

### 日志

运行时会打印类似下面的日志：

```text
2026-04-20 21:02:48,483 INFO [trace_id=...] aiops_agent.cli - CLI started | command=run, trace_id=...
```

### 任务记录

任务会落盘到：

- `storage/tasks/<task_id>.json`

### 审计事件

审计事件会落盘到：

- `storage/audit/events.jsonl`

常见事件包括：

- `task.created`
- `task.started`
- `task.completed`

## 10. 如果想拿到真实执行结果

当前免费版最稳的方式是“先启动，再扩展结果回收”。

如果你后续能让影刀流程执行完成后输出一个 JSON 文件，可以把它接入：

```json
{
  "shadowbot": {
    "result_file": "C:\\temp\\shadowbot-result.json"
  }
}
```

系统会尝试读取这个文件，并按 JSON 内容解析结果。

建议结果文件至少包含这些字段：

```json
{
  "success": true,
  "result": "healthy",
  "anomalies": [],
  "operation_log": [
    "inspection completed"
  ]
}
```

如果文件格式正确，系统就会把“仅启动成功”升级成“拿到真实巡检结果”。

## 11. 常见问题

### 1. 报错：`ShadowBot 免费版本地启动模式仅支持在 Windows 上执行`

原因：

- 当前运行环境不是 Windows

解决：

- 请在 Windows 上执行 CLI

### 2. 报错：`ShadowBot executable_path 未设置`

原因：

- `configs/rpa.json` 中没有填写影刀安装路径

解决：

- 填入真实路径，例如：
  `D:\\Program Files\\ShadowBot\\ShadowBot.exe`

### 3. 报错：`配置缺失: 未找到系统 WebLogic 的巡检流程映射`

原因：

- `inspection.flow_map` 中没有为当前系统配置 UUID

解决：

- 在 `flow_map` 中增加对应项

### 4. 影刀被启动了，但项目没有拿到真实结果

原因：

- 当前默认只负责启动影刀
- 没有配置 `result_file`

解决：

- 让影刀流程执行后输出 JSON 文件
- 再配置 `shadowbot.result_file`

### 5. 启动校验失败

原因：

- 当前项目启用了严格启动校验
- 只要关键配置缺失，CLI 会在启动阶段直接报错

解决：

- 先检查：
  - `execution_mode`
  - `executable_path`
  - `inspection.flow_map`
  - 如果启用了 LLM，再检查 `configs/llm.json`

## 12. 推荐测试顺序

建议按下面顺序测试：

1. 先在 Windows 上手动运行影刀命令，确认本地 ShadowBot 能被启动。
2. 配置 `configs/rpa.json`，把 `execution_mode` 改成 `shadowbot_local`。
3. 暂时关闭 LLM，先用规则解析跑通最小链路。
4. 执行 CLI 命令，确认项目能够成功触发影刀。
5. 检查日志、任务文件和审计文件是否正确生成。
6. 如果需要完整结果，再增加 `result_file` 回收能力。

## 13. 当前状态总结

项目现在已经具备以下能力：

- 在 Windows 上直接启动影刀免费版本地流程
- 支持按系统映射不同影刀机器人 UUID
- 支持 CLI 触发、日志、trace id、任务落盘和审计落盘
- 支持后续扩展结果文件回收

当前最适合的测试目标是：

- 先验证“项目能否从 CLI 成功触发影刀免费版流程”

这一步已经具备条件。
