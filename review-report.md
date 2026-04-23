# opsAgent LangChain/LangGraph Upgrade Review Report

## Overview

This change upgrades `opsAgent` from a phase-1 MVP with a hard-coded single execution path into a more extensible AIOps orchestration kernel. The implementation keeps the existing CLI entrypoint usable while introducing a LangGraph-based workflow, a richer task state model, a session layer, policy gating, structured tool execution contracts, and a LangChain-backed multi-provider LLM adapter.

The goal of this review report is to document:

- what was changed
- why the architecture changed
- what behavior is now supported
- what was verified
- which gaps still remain before the next enterprise-focused phase

## What Was Cleaned Up

The repository contained generated Python cache artifacts that should not be tracked as source changes:

- `__pycache__/` directories
- `*.pyc` files

These files were removed from the working tree, and a new [.gitignore](/Users/randy/Documents/code/opsAgent/.gitignore) was added to prevent them from reappearing in future runs.

## Major Implementation Changes

### 1. Replaced the hard-coded controller flow with a LangGraph orchestration pipeline

The previous controller was effectively:

`parse intent -> if inspection then call inspection tool -> summarize -> persist`

That logic has been replaced in [controller.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/agent/controller.py) with an explicit LangGraph state machine:

- `intent_parse`
- `task_plan`
- `policy_check`
- `tool_execute`
- `summarize`
- `persist_audit`

Benefits of this change:

- makes stage transitions explicit and auditable
- supports non-execution terminal states such as `awaiting_confirmation` and `blocked`
- provides a stable orchestration shell for future capabilities like knowledge retrieval and web automation
- reduces the need to fork execution logic per intent type

### 2. Introduced a richer enterprise-oriented task state model

The previous task model only carried a small set of fields such as `type`, `input`, `status`, and `result`.

In [tasks/models.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/tasks/models.py), the task model now includes:

- `intent`
- `entities`
- `plan`
- `selected_tools`
- `tool_calls`
- `risk_level`
- `confirmation_required`
- `session_id`
- `artifacts`
- `audit_refs`
- `current_stage`
- step budget and confirmation flags

New shared contracts were also introduced:

- `ExecutionPlan`
- `ToolCallSpec`
- `ToolExecutionResult`
- `PolicyDecision`
- `TaskArtifact`

Benefits:

- a single execution state can now represent planning, policy gating, execution, and reporting
- tools can be standardized behind shared request/response contracts
- future integrations can plug in without changing the controller shape again

### 3. Added a planning layer and policy engine

Two new modules were added:

- [planning.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/planning.py)
- [policy.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/policy.py)

The planning layer is intentionally lightweight for phase 1.5:

- `inspection` produces a concrete executable plan with an `inspection` tool call
- `permission_change` produces a high-risk plan that requires confirmation
- `ops_qa` produces a safe placeholder plan
- `web_action` produces a reserved-interface plan and is blocked from auto-execution

The policy engine now decides whether a planned task is:

- approved
- awaiting confirmation
- blocked

Current policy behavior:

- `inspection` can execute automatically
- `permission_change` is routed to `awaiting_confirmation`
- `web_action` is blocked because execution is not yet implemented

Benefits:

- enterprise control is applied before tool execution
- risky tasks can no longer fall through to accidental execution paths
- capability rollout can be staged safely by intent

### 4. Upgraded the tooling model from ad hoc invocation to structured execution

The original `ToolRegistry` was a simple name-to-object map with a direct `execute(tool_name, params)` call.

Now:

- [registry.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/tools/registry.py) stores tool metadata such as risk level, tags, description, and timeout
- [executor.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/tools/executor.py) provides a dedicated tool execution layer
- [inspection.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/tools/inspection.py) now returns `ToolExecutionResult`

Benefits:

- tools now conform to a shared enterprise-style contract
- policy, observability, retries, and future governance can attach to tool calls consistently
- later additions like Playwright tools or knowledge tools can reuse the same execution surface

### 5. Added session persistence

This phase introduces a minimal session layer via:

- [sessions/models.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/sessions/models.py)
- [session_store.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/storage/session_store.py)

The session model currently stores:

- session id
- associated task ids
- last task id
- a simple rolling summary
- metadata and timestamps

CLI support was added for:

- `--session-id`

Benefits:

- tasks can now be grouped under a stable session identity
- later work can extend this into richer memory and context carry-over
- this aligns the current CLI with a future API/service architecture

### 6. Reworked the LLM layer into a LangChain-backed multi-provider adapter

The original implementation coupled intent classification directly to an Anthropic SDK client.

This was replaced with:

- [config.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/config.py): generalized provider-aware LLM config
- [langchain_provider.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/llm/langchain_provider.py): LangChain-based provider adapter
- [factory.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/llm/factory.py): provider construction entrypoint

The new config shape supports:

- `provider`
- `model`
- `role_models`
- `temperature`
- `timeout_seconds`
- `max_retries`
- `fallback_provider`
- `fallback_model`

Current provider support:

- `anthropic`
- `openai`
- `private` as a configuration placeholder for later enterprise adaptation

Benefits:

- avoids a hard dependency on a single vendor-specific implementation
- separates orchestration concerns from model adapter concerns
- creates a natural place for future planning, routing, and summarization model specialization

### 7. Extended CLI without breaking the original entrypoint

The existing command style remains valid:

```bash
aiops-agent run "巡检生产环境 WebLogic"
```

New optional flags were added in [cli.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/cli.py):

- `--session-id`
- `--llm-profile`
- `--max-steps`
- `--require-confirmation`

Benefits:

- preserves backward usability
- adds room for enterprise execution controls
- keeps CLI thin while moving orchestration into reusable service-style logic

### 8. Strengthened audit handling with sanitization

[audit/logger.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/audit/logger.py) now sanitizes sensitive keys before persisting event details.

Fields containing substrings such as:

- `token`
- `password`
- `cookie`
- `secret`

are masked as `***`.

Benefits:

- improves baseline safety for enterprise audit logs
- reduces the chance of leaking secrets into persistent artifacts

## Intent and Behavior Changes

### Supported execution behavior now

- `inspection`
  - fully executable
  - planned and routed through LangGraph
  - policy-approved automatically

- `permission_change`
  - recognized and planned
  - marked as `high_risk_change`
  - transitions into `awaiting_confirmation`
  - does not execute a mutation tool path yet

- `ops_qa`
  - recognized and planned
  - returns a placeholder success response for the unified entry architecture

- `web_action`
  - recognized as a future-facing capability
  - intentionally blocked by policy because browser execution is not yet implemented

### Parser adjustment

[parser.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/agent/parser.py) was updated to add a `web_action` intent while avoiding false positives on strings like `WebLogic`.

This is important because a naive `web` substring rule would have broken the existing inspection and QA behavior for systems whose names begin with `Web`.

## Tests and Verification

Test coverage was kept and extended in [test_agent_flow.py](/Users/randy/Documents/code/opsAgent/tests/test_agent_flow.py).

Verified behaviors:

- inspection success flow still works
- startup validation still rejects invalid LLM configuration
- LLM parser path still runs before rule fallback
- ShadowBot local launch flow still works in the Windows-mocked path
- `permission_change` now enters `awaiting_confirmation`
- `web_action` is now policy-blocked as intended

Validation performed:

```bash
pytest -q
```

Result:

- `11 passed`

## Notable Design Decisions

### Why use LangGraph instead of only LangChain agent abstractions

This implementation uses LangGraph for orchestration rather than relying on a generic agent executor because the project needs:

- explicit stage transitions
- deterministic governance points
- clearer audit boundaries
- enterprise-safe blocking and confirmation states

That makes LangGraph a better fit for controlled AIOps workflows than a looser autonomous agent loop.

### Why `web_action` is blocked instead of partially executed

The plan explicitly treated browser automation as a future capability. The code reflects that safely:

- the interface exists now
- the orchestration path exists now
- the policy engine blocks execution until a browser toolchain and confirmation flow are implemented

This keeps the architecture open without pretending the capability is production-ready.

### Why `ops_qa` is only a placeholder

The upgrade goal in this phase is platform structure, not immediate delivery of every downstream capability. Returning a structured placeholder keeps:

- the unified entrypoint intact
- the orchestration contract stable
- future retrieval integration low-risk

## Remaining Gaps and Risks

This is a meaningful platform upgrade, but it is still not a full enterprise-ready AIOps product. The main remaining gaps are:

### 1. Planning is still mostly rule-based

Although the LLM adapter now supports planning, the active planning behavior in this phase is mostly deterministic and intent-driven. This is good for safety, but it means:

- the system is not yet performing rich multi-step decomposition
- tool routing is still narrow
- enterprise playbooks are not yet encoded as reusable plan policies

### 2. No persistent approval workflow yet

`permission_change` can now stop at a confirmation boundary, but there is no full approval lifecycle:

- no approval token or signed decision record
- no resume-after-approval workflow
- no role-based authorization enforcement

### 3. Session memory is minimal

Sessions currently provide identity and summary storage, but not:

- rich conversational memory
- retrieval-backed context compression
- per-session knowledge accumulation

### 4. No real knowledge retrieval pipeline yet

`ops_qa` still needs:

- retrieval interfaces
- document/source indexing
- answer grounding
- source citation strategy

### 5. No browser automation execution yet

`web_action` is intentionally blocked. To complete that roadmap, the project still needs:

- browser tool abstractions
- page observation schema
- action-risk classification
- human confirmation handoff and resume

### 6. Fallback model handling is configured, not fully implemented

The config now has fields for fallback providers and models, but the actual runtime fallback strategy is still a future enhancement.

## Files Added

New files introduced in this change:

- [.gitignore](/Users/randy/Documents/code/opsAgent/.gitignore)
- [review-report.md](/Users/randy/Documents/code/opsAgent/review-report.md)
- [planning.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/planning.py)
- [policy.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/policy.py)
- [tools/executor.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/tools/executor.py)
- [llm/langchain_provider.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/llm/langchain_provider.py)
- [sessions/models.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/sessions/models.py)
- [sessions/__init__.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/sessions/__init__.py)
- [storage/session_store.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/storage/session_store.py)

## Key Files Modified

Most important modified files:

- [controller.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/agent/controller.py)
- [tasks/models.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/tasks/models.py)
- [cli.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/cli.py)
- [config.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/config.py)
- [registry.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/tools/registry.py)
- [inspection.py](/Users/randy/Documents/code/opsAgent/src/aiops_agent/tools/inspection.py)
- [test_agent_flow.py](/Users/randy/Documents/code/opsAgent/tests/test_agent_flow.py)

## Final Assessment

This upgrade successfully moves `opsAgent` out of the MVP controller pattern and into a much stronger foundation for enterprise AIOps work.

The most important outcomes are:

- the orchestration model is now explicit
- task execution is now policy-aware
- risky workflows can now stop safely
- sessions and structured plans now exist
- the LLM layer is no longer tightly coupled to one provider
- the repository has been cleaned of generated Python cache artifacts

The system is now much closer to a real enterprise kernel, even though several end-user capabilities are still intentionally staged behind placeholder or blocked paths.
