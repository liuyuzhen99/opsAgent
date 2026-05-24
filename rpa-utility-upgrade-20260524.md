# opsAgent RPA 登录能力升级记录（2026-05-24）

## 1. 改造目标

在保留现有巡检能力的基础上，将 opsAgent 从只支持 `inspection` 巡检 RPA，扩展为可按目标系统启动以下登录类 RPA 应用：

- SSH 客户端登录
- SFTP 客户端登录
- 数据库客户端登录

本次为 V1 能力升级，仅负责识别目标并启动对应的登录 RPA，不执行 SSH 命令、SFTP 文件下载或 SQL 查询。

## 2. 用户可用能力

新增 `rpa_action` 意图和工具后，用户可以输入：

```text
登录 120.13 ssh
打开 120.13 的 sftp
登录 120.11 数据库
登录服务器 120.12
```

系统会解析目标和客户端类型，从配置中找到对应 UUID，调用 RPA 平台或在 Windows 上启动 ShadowBot 应用。

在同一 session 中，可继续输入：

```text
再打开这台机器的 sftp
```

opsAgent 会复用最近一次 `rpa_action` 任务记录中的目标机器。

## 3. 实现变更

### 3.1 配置模型与 UUID 映射

`src/aiops_agent/config.py` 新增：

- `RPATargetConfig`：单个目标的 `ssh`、`sftp`、`db` UUID。
- `RPAActionsConfig`：保存全部登录目标映射。
- `RPAConfig.rpa_actions`：承载新增动作配置。

`configs/rpa.json` 新增 `rpa_actions.targets`，同时将巡检流程设置为：

```json
{
  "inspection": {
    "flow_map": {
      "WebLogic": "c95a2e96-2c2e-42db-b168-0d21ecf2f862"
    }
  },
  "shadowbot": {
    "robot_uuid": ""
  }
}
```

`shadowbot.robot_uuid` 置空后，登录流程会始终使用当前目标和能力对应的 UUID，不会被全局值覆盖。

### 3.2 通用 RPA 调用层

新增 `src/aiops_agent/tools/rpa_runner.py`：

- 统一处理 API 模式的 RPA flow 调用。
- 统一处理 Windows `shadowbot_local` 模式启动。
- 统一处理运行配置检查和可选结果文件读取。

`src/aiops_agent/tools/inspection.py` 改为复用 `RPARunner`，其对外巡检输出字段保持不变：

```text
inspection_result
anomalies
operation_log
```

### 3.3 新增登录动作工具

新增 `src/aiops_agent/tools/rpa_action.py`，并在 `src/aiops_agent/cli.py` 注册为 `rpa_action` 工具。

工具输入：

```json
{
  "target": "120.13",
  "capability": "ssh",
  "operation": "login",
  "raw_text": "登录 120.13 ssh"
}
```

支持的 `capability`：

```text
ssh
sftp
db
```

成功返回：

```json
{
  "target": "120.13",
  "capability": "ssh",
  "operation": "login",
  "flow_id": "c33746b0-a09b-469a-bb0d-3dcdf979391c",
  "action_result": "launched",
  "operation_log": []
}
```

若缺少配置，会返回明确错误，例如：

```text
配置缺失: 未配置 120.13 的 db 登录 RPA
```

### 3.4 意图、计划和摘要

`src/aiops_agent/agent/parser.py`：

- 新增 `rpa_action` 识别。
- 支持 `ssh`、`sftp`、`数据库`、`db`、`pl/sql`、`plsql`、`服务器` 关键词。
- “登录服务器”未指定类型时默认使用 `ssh`。
- RPA 登录识别优先于 `web_action`，避免被误判成网页登录任务。

`src/aiops_agent/llm/langchain_provider.py`：

- 将 `rpa_action` 加入 LLM 可返回的 intent 列表及分类提示。

`src/aiops_agent/planning.py`：

- 为 `rpa_action` 创建 `rpa_action/login` 工具调用计划。
- 风险等级为 `controlled_rpa_login`。
- 默认直接执行；显式要求确认时继续沿用现有确认机制。

`src/aiops_agent/agent/summarizer.py`：

- 成功时返回目标、客户端类型、流程 UUID 和启动结果。
- 失败时返回缺失目标或缺失 UUID 的原因。

### 3.5 Session Memory

`SessionTaskIndexEntry` 新增：

```text
target
capability
```

`ContextCompressor` 和 `FileSessionStore` 已同步支持保存、读取和检索这些字段，使后续登录指令能够从当前 session 的历史任务中恢复目标机器。

## 4. 已配置 RPA 应用

| 目标 | SSH UUID | SFTP UUID | DB UUID |
| --- | --- | --- | --- |
| 120.11 | `acc6dd56-29e7-406f-8b24-350f99d749b6` | `c4af5700-a98e-4bbb-9a4e-8216a43f5631` | `58471136-4135-427e-931d-945647517072` |
| 120.12 | `8c43d415-2aa8-4918-9255-d5eabbac42ae` | `72b6231d-2501-4d56-b4ce-3a5214ee9cfe` | - |
| 120.13 | `c33746b0-a09b-469a-bb0d-3dcdf979391c` | `856c1630-7768-446a-8c47-dac6586e55f3` | - |
| 120.14 | `1d931e05-01d5-4cbd-9c78-65836b978f17` | `e5053eb5-c757-4fdd-873e-37555e70bc8c` | - |
| 120.15 | `d3a7e0c9-3906-4a7a-8c8d-e096747225a2` | `c1f24ddb-1cc4-4e39-bf4f-ad435cb36316` | - |
| 120.16 | `b51db932-4018-49f0-a478-5ac55843de5f` | `86a05eb1-911f-4dbd-930f-486b5ff27c9e` | - |
| 120.17 | `9f993758-95e2-4f1a-807c-bca3b416aeea` | `a10a121c-b209-456f-9f10-75542c7a3b8e` | - |
| 120.18 | `e6c958ff-b602-4587-a30b-361c72a2f124` | `68518ab1-c139-43ca-8550-31702a2abbdc` | - |
| 120.19 | `395c2e95-3a72-4c96-a348-b65603dc5761` | `2b304086-14dc-4cd1-ac97-0696093d63bb` | - |
| 120.20 | `1b47d3a3-6565-4f13-bede-835f18f41f18` | `81c46dc1-79ed-46a7-a7c5-e9a2b5d8b545` | - |
| 120.21 | `c792adf6-dfbc-4c3c-8d96-741cabdf6226` | `b8d9db06-08ac-4eeb-80e8-acbd9cc5abbd` | - |
| 120.22 | `95326476-22ba-4ec7-b90e-c80d95ca7558` | `8a230603-2e8a-47ac-b029-6d811041a57d` | - |
| 120.23 | `a9246c32-917b-49a1-9e39-44c980cff213` | `aea13877-1546-4cc8-85a4-7b5fff5eff05` | - |
| 120.24 | `4fc5e758-bdd9-4b4a-8a12-c03bdd6d540c` | `79c65849-9f99-47d3-bc26-d2d5caf2484e` | - |
| 120.25 | `77bc9752-e33f-40d3-96cb-6d838baeb4ff` | `55a7b52f-a658-4d82-8314-e906bfcaf063` | - |
| 120.26 | `41e00883-210a-4588-a1fa-a04ba7be91b7` | `24fbb5c4-61d5-40aa-9946-f17c87e18e4d` | - |
| 120.36 | - | - | `8f5636b6-8b17-4901-91ce-a889f7591318` |
| 217.4 | `0bdcdf88-0cfa-40b0-97e4-3b7640501b13` | - | - |

巡检 UUID：

| 能力 | UUID |
| --- | --- |
| `inspection` | `c95a2e96-2c2e-42db-b168-0d21ecf2f862` |

## 5. 测试与验证

新增或调整测试覆盖：

- 规则解析 SSH、SFTP、DB 登录指令。
- 生成 `rpa_action/login` 执行计划。
- 从 session memory 复用目标机器。
- API 模式按配置 UUID 调用目标 flow。
- ShadowBot local 模式使用目标能力 UUID，而不是全局 UUID。
- 缺少 DB 等客户端配置时返回清晰错误。
- 原有巡检执行、确认机制、知识库和网页任务回归不受影响。

执行结果：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest
```

```text
167 passed, 6 skipped, 2 warnings
```

警告为既有依赖提示，包括 LangGraph 序列化器待弃用默认值提示，以及 `claude-sonnet-4-20250514` 将于 2026-06-15 停止支持的提示，不影响本次 RPA 登录能力测试结果。

## 6. V1 边界与后续方向

本次能力边界：

- 可启动已配置的 SSH、SFTP、DB 登录 RPA。
- 不执行登录后的命令输入、文件下载、SQL 执行和结果采集。
- 实际 PuTTY、FileZilla、PL/SQL 登录效果需在安装了 ShadowBot 的 Windows 环境验证。

下一阶段可基于 `rpa_action` 继续扩展：

- `ssh_run_command`
- `sftp_download_file`
- `db_execute_query`
- RPA 登录成功/失败结果回收和审计输出
