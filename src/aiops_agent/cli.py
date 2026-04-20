from __future__ import annotations

import argparse
import logging

from aiops_agent.agent.controller import AgentController
from aiops_agent.agent.parser import IntentParser
from aiops_agent.agent.summarizer import ResultSummarizer
from aiops_agent.audit.logger import FileAuditLogger
from aiops_agent.config import (
    ConfigError,
    load_anthropic_config,
    load_rpa_config,
    validate_startup_config,
)
from aiops_agent.llm.client import create_llm_provider
from aiops_agent.storage.task_store import FileTaskStore
from aiops_agent.support.logging import configure_logging, get_logger, log_kv
from aiops_agent.support.trace import generate_trace_id, set_trace_id
from aiops_agent.tasks.manager import TaskManager
from aiops_agent.tools.inspection import InspectionTool
from aiops_agent.tools.registry import ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiops-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a natural language ops task")
    run_parser.add_argument("task_input", help="Natural language task description")
    run_parser.add_argument(
        "--config",
        dest="config_path",
        help="Optional RPA config file path",
    )
    run_parser.add_argument(
        "--llm-config",
        dest="llm_config_path",
        help="Optional LLM config file path",
    )
    run_parser.add_argument(
        "--log-level",
        dest="log_level",
        default="INFO",
        help="Runtime log level",
    )
    return parser


def create_controller(
    config_path: str | None = None,
    llm_config_path: str | None = None,
    llm_provider=None,
) -> AgentController:
    rpa_config = load_rpa_config(config_path)
    anthropic_config = load_anthropic_config(llm_config_path)
    validate_startup_config(rpa_config, anthropic_config)
    registry = ToolRegistry()
    registry.register("inspection", InspectionTool(rpa_config))

    store = FileTaskStore()
    audit_logger = FileAuditLogger()
    manager = TaskManager(store=store)
    provider = llm_provider or create_llm_provider(anthropic_config)
    return AgentController(
        parser=IntentParser(rpa_config=rpa_config, llm_provider=provider),
        task_manager=manager,
        tool_registry=registry,
        summarizer=ResultSummarizer(),
        audit_logger=audit_logger,
        logger=get_logger(__name__),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "run":
        parser.error(f"Unsupported command: {args.command}")

    configure_logging(args.log_level.upper())
    trace_id = generate_trace_id()
    set_trace_id(trace_id)
    logger = get_logger(__name__)
    log_kv(logger, logging.INFO, "CLI started", command=args.command, trace_id=trace_id)

    try:
        controller = create_controller(args.config_path, args.llm_config_path)
        task = controller.run(args.task_input)
    except ConfigError as exc:
        print(f"配置错误: {exc}")
        log_kv(logger, logging.ERROR, "Startup validation failed", error=str(exc))
        return 2

    print(task.report or "")
    return 0 if task.status == "success" else 1
