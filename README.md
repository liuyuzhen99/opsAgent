# opsAgent

中文 | [English](#english)

opsAgent 是一个面向企业运维场景的受控 AIOps Agent 运行时。它可以从 CLI 或交互式 Chat 接收自然语言任务，将任务转换为结构化执行计划，经过策略检查后调用受控工具，并把任务状态、会话上下文、审计事件和浏览器执行产物持久化到本地。

核心原则：LLM 参与理解、规划、总结、知识库合成和浏览器下一步决策，但不直接执行高权限动作。所有真实执行都必须经过工具协议、策略门禁、浏览器风险检查和可选人工确认。

## 功能特性

- 通过 `aiops-agent run` 执行单次自然语言运维任务。
- 通过 `aiops-agent chat` 进行多轮交互式运维对话。
- 支持 RPA 巡检流程，可对接 API 或本地 ShadowBot。
- 基于 Playwright 的受控浏览器自动化。
- 对高风险或远端写入类浏览器动作提供人工确认门禁。
- 支持 Obsidian vault 的索引、检索、问答和结构化笔记写入。
- 可从成功浏览器任务沉淀 Web Skill，并在后续相似任务中复用。
- 本地任务、会话和 JSONL 审计日志持久化。
- 支持 Anthropic 与 OpenAI 兼容 LLM Provider 配置。

## 架构概览

opsAgent 采用分层运行时设计：

1. 接入层：CLI 与 Chat。
2. 配置层：RPA、LLM、凭据、浏览器站点和知识库配置。
3. Agent 编排层：意图识别、计划生成、策略检查、工具执行、结果总结和持久化。
4. 工具协议层：`Task`、`ExecutionPlan`、`ToolCallSpec`、工具注册表和执行器。
5. 能力层：RPA、知识库、浏览器自动化、Chat 和 Web Skill。
6. 支撑层：本地存储、会话、审计日志、Trace 和 Artifacts。

主链路：

```text
用户输入 -> 意图识别 -> 执行计划 -> 策略检查 -> 工具执行 -> 结果总结 -> 审计/会话/任务持久化
```

更完整的技术说明见 [docs/opsAgent-architecture-summary.md](docs/opsAgent-architecture-summary.md)。

## 环境要求

- Python 3.10 或更高版本
- 浏览器自动化需要 Playwright 浏览器运行时
- 可选：Anthropic 或 OpenAI 兼容 API Key，用于 LLM 规划和合成
- 可选：RPA 平台凭据或本地 ShadowBot 配置
- 可选：用于知识检索和写入的 Obsidian vault

## 安装

```bash
git clone <repo-url>
cd opsAgent
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m playwright install chromium
```

运行测试：

```bash
python -m pytest
```

如果本机没有 `python` 命令，可改用：

```bash
python3 -m pytest
```

## 配置

运行时配置不应提交到版本库。仓库已忽略 `configs/` 和 `storage/`，因为这些目录可能包含凭据、本地任务历史、浏览器状态、截图、Trace、企业内部知识库路径或其他敏感上下文。

按需在本地创建以下文件。

### `configs/rpa.json`

```json
{
  "provider": "example-rpa",
  "execution_mode": "api",
  "platform_url": "https://rpa.example.com",
  "timeout_seconds": 30,
  "auth": {
    "type": "bearer",
    "token": "replace-with-token"
  },
  "inspection": {
    "default_system": "WebLogic",
    "default_env": "prod",
    "flow_map": {
      "WebLogic": "replace-with-flow-id"
    }
  },
  "knowledge": {
    "vault_path": "/path/to/obsidian/vault",
    "index_mode": "keyword",
    "include_patterns": ["*.md"],
    "exclude_patterns": [".obsidian/**", "attachments/**", "archive/**", "secrets/**"]
  }
}
```

如需本地 ShadowBot 执行，可设置 `"execution_mode": "shadowbot_local"`，并提供 `shadowbot` 配置。

### `configs/llm.json`

```json
{
  "enabled": true,
  "provider": "anthropic",
  "model": "claude-sonnet-4-20250514",
  "api_key": "replace-with-api-key",
  "timeout_seconds": 20,
  "max_retries": 2,
  "max_tokens": 512,
  "temperature": 0.0
}
```

也可以通过环境变量提供密钥：

```bash
export AIOPS_LLM_ENABLED=true
export ANTHROPIC_API_KEY=replace-with-api-key
export ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

OpenAI 兼容 Provider 示例：

```json
{
  "enabled": true,
  "provider": "openai",
  "model": "gpt-4.1-mini",
  "api_key": "replace-with-api-key",
  "base_url": "https://api.openai.com/v1"
}
```

### `configs/browser_sites.json`

```json
{
  "sites": {
    "admin": {
      "site_key": "admin",
      "base_url": "https://admin.example.com",
      "login_url": "https://admin.example.com/login",
      "allowed_domains": ["admin.example.com"],
      "login_fields": {
        "username": "input[name='username']",
        "password": "input[name='password']",
        "submit": "button[type='submit']"
      },
      "workflows": {}
    }
  }
}
```

### `configs/credentials.local.json`

```json
{
  "credentials": {
    "admin": {
      "username": "replace-with-username",
      "password": "replace-with-password"
    }
  }
}
```

该文件只应保存在本地，不能提交真实凭据。

## CLI 用法

执行单次任务：

```bash
aiops-agent run "Inspect the WebLogic production environment"
```

启动交互式 Chat：

```bash
aiops-agent chat
```

以可视浏览器执行受控 Web 工作流：

```bash
aiops-agent run "Search for user alice in the admin portal" \
  --browser-site admin \
  --credential-ref admin \
  --allowed-domains admin.example.com \
  --headed
```

恢复等待确认的任务：

```bash
aiops-agent confirm <task-id>
```

构建或重建知识库索引：

```bash
aiops-agent knowledge index --force
```

直接查询知识库：

```bash
aiops-agent knowledge query "How do we handle WebLogic high CPU incidents?"
```

写入结构化知识笔记：

```bash
aiops-agent knowledge write "Save the current troubleshooting steps as a runbook note"
```

管理本地会话：

```bash
aiops-agent session list --all
aiops-agent session close <session-id>
```

## 本地存储

默认情况下，opsAgent 会把运行状态写入 `storage/`：

- `storage/tasks/`：任务状态、执行计划、执行结果和 artifacts 元数据。
- `storage/sessions/`：会话记忆和浏览器连续性状态。
- `storage/audit/events.jsonl`：计划、策略、工具调用、浏览器动作和页面观察审计事件。
- `storage/artifacts/`：截图、页面摘要、浏览器 state、Trace、Video 和报告。
- `storage/web_skills/`：生成的可复用 Web Skill。

这些文件已被 git 忽略。分享前仍应检查，因为其中可能包含运维上下文。

## 开发

```bash
pip install -e .
python -m pytest
```

主要代码目录：

- `src/aiops_agent/cli.py`：CLI 入口和运行时装配。
- `src/aiops_agent/agent/`：Controller、Parser、上下文压缩、进度和总结。
- `src/aiops_agent/tools/`：工具协议和工具实现。
- `src/aiops_agent/browser/`：Playwright 浏览器 Agent、规划、风险检查、凭据和站点配置。
- `src/aiops_agent/knowledge/`：vault 索引、检索、评估和笔记写入。
- `tests/`：Agent、浏览器规划、知识库、会话、凭据和策略相关测试。

## 安全说明

- 不要提交 `configs/`、`storage/`、浏览器 Trace、Video、截图、vault 内容或真实凭据。
- 使用 `allowed_domains` 和浏览器站点配置限制浏览器自动化范围。
- 对需要人工复核的流程启用 `--require-confirmation`。
- 将审计日志和 artifacts 视为敏感运维记录。
- 在生产流程中复用 Web Skill 前应人工审查。

## 项目状态

当前仓库是 MVP 实现，重点是验证受控 AIOps 编排模式和本地运行闭环，不是开箱即用的生产平台。生产使用前应补充环境级别的密钥管理、审批流、部署隔离、可观测性和访问控制。

---

## English

opsAgent is a controlled AIOps agent runtime for enterprise operations workflows. It accepts natural language requests from a CLI or interactive chat session, converts them into structured execution plans, applies policy checks, runs approved tools, and persists task state, session context, audit events, and browser artifacts locally.

The core principle is simple: the LLM helps with understanding, planning, summarization, knowledge synthesis, and browser step selection, but it does not execute privileged actions directly. Real execution goes through typed tool contracts, policy gates, browser risk checks, and optional human confirmation.

## Features

- Run one-off natural language operations tasks with `aiops-agent run`.
- Use multi-turn interactive operations chat with `aiops-agent chat`.
- Integrate RPA inspection workflows through an API or local ShadowBot execution.
- Run controlled Playwright browser automation for web operations tasks.
- Require human confirmation for risky or remote state-changing browser actions.
- Index, retrieve, answer questions from, and write curated notes into an Obsidian vault.
- Capture successful browser workflows as reusable Web Skills.
- Persist local task state, session memory, and JSONL audit logs.
- Configure Anthropic or OpenAI-compatible LLM providers.

## Architecture

opsAgent is organized as a layered runtime:

1. Entry layer: CLI and chat interfaces.
2. Configuration layer: RPA, LLM, credentials, browser site, and knowledge settings.
3. Agent orchestration layer: intent parsing, planning, policy checks, tool execution, summarization, and persistence.
4. Tool protocol layer: `Task`, `ExecutionPlan`, `ToolCallSpec`, registry, and executor contracts.
5. Capability layer: RPA tools, knowledge tools, browser automation, chat, and Web Skills.
6. Support layer: local storage, sessions, audit logs, traces, and artifacts.

Main flow:

```text
user input -> intent parsing -> execution plan -> policy check -> tool execution -> result summary -> audit/session/task persistence
```

For a deeper technical overview, see [docs/opsAgent-architecture-summary.md](docs/opsAgent-architecture-summary.md).

## Requirements

- Python 3.10 or newer
- Playwright browser runtime for browser automation
- Optional: an Anthropic or OpenAI-compatible API key for LLM planning and synthesis
- Optional: RPA platform credentials or local ShadowBot configuration
- Optional: an Obsidian vault for knowledge retrieval and writing

## Installation

```bash
git clone <repo-url>
cd opsAgent
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m playwright install chromium
```

Run tests:

```bash
python -m pytest
```

If `python` is not available on your machine, use:

```bash
python3 -m pytest
```

## Configuration

Runtime configuration should stay out of version control. This repository ignores `configs/` and `storage/` because these folders may contain credentials, local task history, browser state, screenshots, traces, company-specific knowledge paths, or other sensitive context.

Create the following files locally as needed.

### `configs/rpa.json`

```json
{
  "provider": "example-rpa",
  "execution_mode": "api",
  "platform_url": "https://rpa.example.com",
  "timeout_seconds": 30,
  "auth": {
    "type": "bearer",
    "token": "replace-with-token"
  },
  "inspection": {
    "default_system": "WebLogic",
    "default_env": "prod",
    "flow_map": {
      "WebLogic": "replace-with-flow-id"
    }
  },
  "knowledge": {
    "vault_path": "/path/to/obsidian/vault",
    "index_mode": "keyword",
    "include_patterns": ["*.md"],
    "exclude_patterns": [".obsidian/**", "attachments/**", "archive/**", "secrets/**"]
  }
}
```

For local ShadowBot execution, set `"execution_mode": "shadowbot_local"` and provide the `shadowbot` section.

### `configs/llm.json`

```json
{
  "enabled": true,
  "provider": "anthropic",
  "model": "claude-sonnet-4-20250514",
  "api_key": "replace-with-api-key",
  "timeout_seconds": 20,
  "max_retries": 2,
  "max_tokens": 512,
  "temperature": 0.0
}
```

Secrets can also be supplied with environment variables:

```bash
export AIOPS_LLM_ENABLED=true
export ANTHROPIC_API_KEY=replace-with-api-key
export ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

OpenAI-compatible provider example:

```json
{
  "enabled": true,
  "provider": "openai",
  "model": "gpt-4.1-mini",
  "api_key": "replace-with-api-key",
  "base_url": "https://api.openai.com/v1"
}
```

### `configs/browser_sites.json`

```json
{
  "sites": {
    "admin": {
      "site_key": "admin",
      "base_url": "https://admin.example.com",
      "login_url": "https://admin.example.com/login",
      "allowed_domains": ["admin.example.com"],
      "login_fields": {
        "username": "input[name='username']",
        "password": "input[name='password']",
        "submit": "button[type='submit']"
      },
      "workflows": {}
    }
  }
}
```

### `configs/credentials.local.json`

```json
{
  "credentials": {
    "admin": {
      "username": "replace-with-username",
      "password": "replace-with-password"
    }
  }
}
```

Keep this file local only. Do not commit real credentials.

## CLI Usage

Run a single task:

```bash
aiops-agent run "Inspect the WebLogic production environment"
```

Start interactive chat:

```bash
aiops-agent chat
```

Run a controlled browser workflow with a visible browser:

```bash
aiops-agent run "Search for user alice in the admin portal" \
  --browser-site admin \
  --credential-ref admin \
  --allowed-domains admin.example.com \
  --headed
```

Resume a task waiting for confirmation:

```bash
aiops-agent confirm <task-id>
```

Build or rebuild the knowledge index:

```bash
aiops-agent knowledge index --force
```

Query the knowledge vault directly:

```bash
aiops-agent knowledge query "How do we handle WebLogic high CPU incidents?"
```

Write a curated note:

```bash
aiops-agent knowledge write "Save the current troubleshooting steps as a runbook note"
```

Manage local sessions:

```bash
aiops-agent session list --all
aiops-agent session close <session-id>
```

## Local Storage

By default, opsAgent writes runtime state under `storage/`:

- `storage/tasks/`: task state, execution plans, results, and artifacts metadata.
- `storage/sessions/`: chat/session memory and browser continuity state.
- `storage/audit/events.jsonl`: audit events for plans, policy decisions, tool calls, browser actions, and observations.
- `storage/artifacts/`: screenshots, page summaries, browser state, traces, videos, and reports.
- `storage/web_skills/`: generated reusable Web Skills.

These files are ignored by git. Review them before sharing because they may contain operational context.

## Development

```bash
pip install -e .
python -m pytest
```

Useful code areas:

- `src/aiops_agent/cli.py`: CLI entry points and runtime assembly.
- `src/aiops_agent/agent/`: controller, parser, context compression, progress, and summarization.
- `src/aiops_agent/tools/`: tool protocol implementations.
- `src/aiops_agent/browser/`: Playwright browser agent, planning, risk checks, credentials, and site config.
- `src/aiops_agent/knowledge/`: vault indexing, retrieval, evaluation, and note writing.
- `tests/`: tests for agent flow, browser planning, knowledge tooling, sessions, credentials, and policies.

## Security Notes

- Do not commit `configs/`, `storage/`, browser traces, videos, screenshots, vault contents, or real credentials.
- Use `allowed_domains` and browser site configuration to constrain browser automation.
- Enable `--require-confirmation` for workflows that require manual review.
- Treat audit logs and artifacts as sensitive operational records.
- Review Web Skills before reusing them in production workflows.

## Project Status

This repository is an MVP implementation. The current focus is a controlled local runtime for validating AIOps orchestration patterns, not a turnkey production platform. Production use should add environment-specific hardening around secrets management, approval workflows, deployment isolation, observability, and access control.

