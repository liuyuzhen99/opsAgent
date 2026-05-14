from __future__ import annotations

import argparse
import logging

from aiops_agent.agent.controller import AgentController
from aiops_agent.agent.parser import IntentParser
from aiops_agent.agent.summarizer import ResultSummarizer
from aiops_agent.audit.logger import FileAuditLogger
from aiops_agent.browser.agent import BrowserAgentTool
from aiops_agent.browser.credentials import CredentialError, CredentialStore
from aiops_agent.browser.planner import BrowserPlanner
from aiops_agent.browser.skills import WebSkillGenerator, WebSkillMatcher, WebSkillStore
from aiops_agent.browser.site_config import BrowserSiteConfigError, load_browser_sites_config
from aiops_agent.chat import ChatOptions, ChatRunner
from aiops_agent.config import (
    ConfigError,
    load_anthropic_config,
    load_rpa_config,
    validate_startup_config,
)
from aiops_agent.llm.client import create_llm_provider
from aiops_agent.planning import PlanningService
from aiops_agent.storage.session_store import FileSessionStore
from aiops_agent.storage.task_store import FileTaskStore
from aiops_agent.support.logging import configure_logging, get_logger, log_kv
from aiops_agent.support.trace import generate_trace_id, set_trace_id
from aiops_agent.tasks.manager import TaskManager
from aiops_agent.tools.executor import ToolExecutor
from aiops_agent.tools.chat import ChatTool
from aiops_agent.tools.inspection import InspectionTool
from aiops_agent.tools.knowledge import KnowledgeTool, KnowledgeWriteTool
from aiops_agent.tools.registry import ToolRegistry


DEFAULT_BROWSER_MAX_STEPS = 40
DEFAULT_BROWSER_SLOW_MO_MS = 300


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
        "--credential-config",
        dest="credential_config_path",
        help="Optional local browser credential config file path",
    )
    run_parser.add_argument(
        "--credential-ref",
        dest="credential_ref",
        help="Credential reference key for browser login tasks",
    )
    run_parser.add_argument(
        "--browser-site",
        dest="browser_site",
        help="Configured browser site key for account and permission web workflows",
    )
    run_parser.add_argument(
        "--browser-sites-config",
        dest="browser_sites_config_path",
        help="Optional browser sites config file path",
    )
    run_parser.add_argument(
        "--log-level",
        dest="log_level",
        default="INFO",
        help="Runtime log level",
    )
    run_parser.add_argument("--session-id", dest="session_id", help="Optional session ID")
    run_parser.add_argument(
        "--llm-profile",
        dest="llm_profile",
        help="Optional LLM profile name",
    )
    run_parser.add_argument(
        "--max-steps",
        dest="max_steps",
        type=int,
        default=DEFAULT_BROWSER_MAX_STEPS,
        help="Execution step budget",
    )
    run_parser.add_argument(
        "--allowed-domains",
        dest="allowed_domains",
        help="Comma-separated domain allowlist for browser tasks",
    )
    run_parser.add_argument(
        "--headed",
        dest="headed",
        action="store_true",
        help="Run browser tasks with a visible browser window",
    )
    run_parser.add_argument(
        "--browser-trace",
        dest="browser_trace",
        action="store_true",
        help="Save Playwright trace for browser tasks",
    )
    run_parser.add_argument(
        "--browser-video",
        dest="browser_video",
        action="store_true",
        help="Save Playwright video for browser tasks",
    )
    run_parser.add_argument(
        "--browser-channel",
        dest="browser_channel",
        choices=("chromium", "msedge", "chrome"),
        default="chromium",
        help="Browser channel for Playwright browser tasks",
    )
    run_parser.add_argument(
        "--browser-slow-mo",
        dest="browser_slow_mo_ms",
        type=int,
        default=DEFAULT_BROWSER_SLOW_MO_MS,
        help="Slow down Playwright browser actions by this many milliseconds",
    )
    run_parser.add_argument(
        "--require-confirmation",
        dest="require_confirmation",
        action="store_true",
        help="Force manual confirmation before execution",
    )
    chat_parser = subparsers.add_parser("chat", help="Start an interactive Agent chat session")
    chat_parser.add_argument("--config", dest="config_path", help="Optional RPA config file path")
    chat_parser.add_argument("--llm-config", dest="llm_config_path", help="Optional LLM config file path")
    chat_parser.add_argument("--credential-config", dest="credential_config_path", help="Optional local browser credential config file path")
    chat_parser.add_argument("--credential-ref", dest="credential_ref", help="Credential reference key for browser login tasks")
    chat_parser.add_argument("--browser-site", dest="browser_site", help="Configured browser site key for account and permission web workflows")
    chat_parser.add_argument("--browser-sites-config", dest="browser_sites_config_path", help="Optional browser sites config file path")
    chat_parser.add_argument("--log-level", dest="log_level", default="INFO", help="Runtime log level")
    chat_parser.add_argument("--session-id", dest="session_id", help="Optional session ID")
    chat_parser.add_argument("--llm-profile", dest="llm_profile", help="Optional LLM profile name")
    chat_parser.add_argument("--max-steps", dest="max_steps", type=int, default=DEFAULT_BROWSER_MAX_STEPS, help="Execution step budget")
    chat_parser.add_argument("--allowed-domains", dest="allowed_domains", help="Comma-separated domain allowlist for browser tasks")
    chat_parser.add_argument("--headed", dest="headed", action="store_true", help="Run browser tasks with a visible browser window")
    chat_parser.add_argument("--browser-trace", dest="browser_trace", action="store_true", help="Save Playwright trace for browser tasks")
    chat_parser.add_argument("--browser-video", dest="browser_video", action="store_true", help="Save Playwright video for browser tasks")
    chat_parser.add_argument(
        "--browser-channel",
        dest="browser_channel",
        choices=("chromium", "msedge", "chrome"),
        default="chromium",
        help="Browser channel for Playwright browser tasks",
    )
    chat_parser.add_argument(
        "--browser-slow-mo",
        dest="browser_slow_mo_ms",
        type=int,
        default=DEFAULT_BROWSER_SLOW_MO_MS,
        help="Slow down Playwright browser actions by this many milliseconds",
    )
    chat_parser.add_argument(
        "--require-confirmation",
        dest="require_confirmation",
        action="store_true",
        help="Force manual confirmation before execution",
    )
    confirm_parser = subparsers.add_parser("confirm", help="Confirm and resume a blocked browser action")
    confirm_parser.add_argument("task_id", help="Task ID waiting for confirmation")
    confirm_parser.add_argument("--config", dest="config_path", help="Optional RPA config file path")
    confirm_parser.add_argument("--llm-config", dest="llm_config_path", help="Optional LLM config file path")
    confirm_parser.add_argument("--credential-config", dest="credential_config_path", help="Optional local browser credential config file path")
    confirm_parser.add_argument("--browser-sites-config", dest="browser_sites_config_path", help="Optional browser sites config file path")
    confirm_parser.add_argument("--log-level", dest="log_level", default="INFO", help="Runtime log level")
    knowledge_parser = subparsers.add_parser("knowledge", help="Manage Obsidian vault knowledge index")
    knowledge_subparsers = knowledge_parser.add_subparsers(dest="knowledge_command", required=True)
    knowledge_parser.add_argument("--config", dest="config_path", help="Optional RPA config file path")
    knowledge_parser.add_argument("--llm-config", dest="llm_config_path", help="Optional LLM config file path")
    knowledge_parser.add_argument("--log-level", dest="log_level", default="INFO", help="Runtime log level")
    knowledge_index_parser = knowledge_subparsers.add_parser("index", help="Build or rebuild vault index")
    knowledge_index_parser.add_argument("--force", action="store_true", help="Force rebuild even if index is current")
    knowledge_query_parser = knowledge_subparsers.add_parser("query", help="Query vault directly (bypasses agent)")
    knowledge_query_parser.add_argument("question", help="Question to query")
    knowledge_write_parser = knowledge_subparsers.add_parser("write", help="Write a curated note into the configured vault")
    knowledge_write_parser.add_argument("instruction", help="Instruction or note context to save")
    knowledge_write_parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Preview target metadata without writing")
    session_parser = subparsers.add_parser("session", help="Manage local Agent sessions")
    session_subparsers = session_parser.add_subparsers(dest="session_command", required=True)
    list_parser = session_subparsers.add_parser("list", help="List sessions")
    list_parser.add_argument("--all", dest="all_sessions", action="store_true", help="Include closed sessions")
    close_parser = session_subparsers.add_parser("close", help="Close a session")
    close_parser.add_argument("session_id", help="Session ID to close")
    return parser


def create_controller(
    config_path: str | None = None,
    llm_config_path: str | None = None,
    llm_provider=None,
    browser_headless: bool = True,
    credential_config_path: str | None = None,
    browser_sites_config_path: str | None = None,
) -> AgentController:
    rpa_config = load_rpa_config(config_path)
    anthropic_config = load_anthropic_config(llm_config_path)
    validate_startup_config(rpa_config, anthropic_config)
    registry = ToolRegistry()
    from aiops_agent.knowledge.engine import KnowledgeEngine
    knowledge_engine = KnowledgeEngine(rpa_config.knowledge, anthropic_config)
    registry.register(
        "inspection",
        InspectionTool(rpa_config),
        risk_level="read_only",
        description="Run structured inspection flow via RPA",
        tags=["inspection", "rpa"],
        timeout_seconds=rpa_config.timeout_seconds,
    )
    registry.register(
        "knowledge",
        KnowledgeTool(rpa_config.knowledge, llm_config=anthropic_config, engine=knowledge_engine),
        risk_level="read_only",
        description="Obsidian vault knowledge query via BM25/vector + LLM synthesis",
        tags=["knowledge", "obsidian", "ops_qa"],
    )
    registry.register(
        "knowledge_writer",
        KnowledgeWriteTool(rpa_config.knowledge, llm_config=anthropic_config, engine=knowledge_engine),
        risk_level="controlled_change",
        description="Write curated Obsidian notes and update MOC",
        tags=["knowledge", "obsidian", "knowledge_write"],
    )

    store = FileTaskStore()
    session_store = FileSessionStore()
    audit_logger = FileAuditLogger()
    try:
        credential_store = CredentialStore(credential_config_path)
    except CredentialError as exc:
        raise ConfigError(str(exc)) from exc
    try:
        browser_sites_config = load_browser_sites_config(browser_sites_config_path)
    except BrowserSiteConfigError as exc:
        raise ConfigError(str(exc)) from exc
    provider = llm_provider or create_llm_provider(anthropic_config)
    web_skill_store = WebSkillStore()
    web_skill_matcher = WebSkillMatcher(web_skill_store)
    web_skill_generator = WebSkillGenerator(web_skill_store)
    registry.register(
        "chat",
        ChatTool(provider),
        risk_level="read_only",
        description="Reply to non-task interactive chat messages",
        tags=["chat", "interactive"],
    )
    registry.register(
        "browser_agent",
        BrowserAgentTool(
            audit_logger=audit_logger,
            headless=browser_headless,
            credential_store=credential_store,
            planner=BrowserPlanner(llm_provider=provider),
        ),
        risk_level="controlled_browser",
        description="Run bounded Playwright browser actions with confirmation gates",
        tags=["browser", "playwright", "web_action"],
    )
    manager = TaskManager(store=store)
    return AgentController(
        parser=IntentParser(rpa_config=rpa_config, llm_provider=provider),
        task_manager=manager,
        tool_executor=ToolExecutor(registry),
        summarizer=ResultSummarizer(),
        audit_logger=audit_logger,
        session_store=session_store,
        planning_service=PlanningService(web_skill_matcher=web_skill_matcher),
        browser_sites_config=browser_sites_config,
        web_skill_generator=web_skill_generator,
        credential_ref_resolver=credential_store.default_ref_for_site,
        logger=get_logger(__name__),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "session":
        store = FileSessionStore()
        if args.session_command == "list":
            for session in store.list(active_only=not args.all_sessions):
                print(f"{session.id}\t{session.status}\t{session.last_task_id or '-'}\t{session.summary}")
            return 0
        if args.session_command == "close":
            session = store.close(args.session_id)
            if session is None:
                print(f"会话不存在: {args.session_id}")
                return 1
            print(f"已关闭会话: {session.id}")
            return 0

    if args.command == "knowledge":
        configure_logging(args.log_level.upper())
        try:
            rpa_config = load_rpa_config(args.config_path)
            anthropic_config = load_anthropic_config(args.llm_config_path)
        except ConfigError as exc:
            print(f"配置错误: {exc}")
            return 2
        from aiops_agent.knowledge.engine import KnowledgeEngine
        engine = KnowledgeEngine(rpa_config.knowledge, anthropic_config)
        if args.knowledge_command == "index":
            force = getattr(args, "force", False)
            print("正在构建知识库索引..." + ("（强制重建）" if force else ""))
            try:
                engine.rebuild_index(force=force)
                print("索引构建完成。")
            except Exception as exc:
                print(f"索引构建失败: {exc}")
                return 1
            return 0
        if args.knowledge_command == "query":
            try:
                answer = engine.query(args.question)
            except Exception as exc:
                print(f"查询失败: {exc}")
                return 1
            print(answer.answer)
            if answer.sources:
                print("\n来源文档：")
                for src in answer.sources:
                    print(f"  - {src.title} ({src.section})")
            return 0
        if args.knowledge_command == "write":
            tool = KnowledgeWriteTool(rpa_config.knowledge, anthropic_config, engine=engine)
            result = tool.execute({"instruction": args.instruction, "dry_run": bool(args.dry_run)})
            data = result.data or {}
            if result.success:
                prefix = "Dry-run 预览完成" if args.dry_run else "知识笔记写入完成"
                print(f"{prefix}: {data.get('title')}")
                print(f"笔记路径: {data.get('note_path')}")
                print(f"类型: {data.get('type')}")
                print(f"MOC: {data.get('moc_path') or '-'}")
                print(f"索引状态: {data.get('reindex_status')}")
                return 0
            print(f"写入失败: {result.error or '未知错误'}")
            missing = data.get("missing_info") or []
            if missing:
                print("缺失配置: " + ", ".join(missing))
            if data.get("note_path"):
                print(f"目标路径: {data.get('note_path')}")
            return 1
        return 0

    if args.command == "confirm":
        configure_logging(args.log_level.upper())
        trace_id = generate_trace_id()
        set_trace_id(trace_id)
        try:
            controller = create_controller(
                args.config_path,
                args.llm_config_path,
                credential_config_path=args.credential_config_path,
                browser_sites_config_path=args.browser_sites_config_path,
            )
            task = controller.confirm(args.task_id)
        except (ConfigError, ValueError) as exc:
            print(f"确认恢复失败: {exc}")
            return 2
        print(task.report or "")
        return 0 if task.status == "success" else 1

    if args.command == "chat":
        configure_logging(args.log_level.upper())
        trace_id = generate_trace_id()
        set_trace_id(trace_id)
        logger = get_logger(__name__)
        log_kv(logger, logging.INFO, "CLI started", command=args.command, trace_id=trace_id)
        try:
            controller = create_controller(
                args.config_path,
                args.llm_config_path,
                browser_headless=not args.headed,
                credential_config_path=args.credential_config_path,
                browser_sites_config_path=args.browser_sites_config_path,
            )
        except ConfigError as exc:
            print(f"配置错误: {exc}")
            log_kv(logger, logging.ERROR, "Startup validation failed", error=str(exc))
            return 2
        runner = ChatRunner(
            controller,
            ChatOptions(
                session_id=args.session_id,
                llm_profile=args.llm_profile,
                max_steps=args.max_steps,
                require_confirmation=args.require_confirmation,
                credential_ref=args.credential_ref,
                browser_trace=args.browser_trace,
                browser_video=args.browser_video,
                browser_site=args.browser_site,
                browser_channel=args.browser_channel,
                browser_slow_mo_ms=args.browser_slow_mo_ms,
                allowed_domains=[
                    domain.strip()
                    for domain in (args.allowed_domains or "").split(",")
                    if domain.strip()
                ],
            ),
        )
        return runner.run()

    if args.command != "run":
        parser.error(f"Unsupported command: {args.command}")

    configure_logging(args.log_level.upper())
    trace_id = generate_trace_id()
    set_trace_id(trace_id)
    logger = get_logger(__name__)
    log_kv(logger, logging.INFO, "CLI started", command=args.command, trace_id=trace_id)

    try:
        controller = create_controller(
            args.config_path,
            args.llm_config_path,
            browser_headless=not args.headed,
            credential_config_path=args.credential_config_path,
            browser_sites_config_path=args.browser_sites_config_path,
        )
        task = controller.run(
            args.task_input,
            session_id=args.session_id,
            llm_profile=args.llm_profile,
            max_steps=args.max_steps,
            require_confirmation=args.require_confirmation,
            credential_ref=args.credential_ref,
            browser_trace=args.browser_trace,
            browser_video=args.browser_video,
            browser_site=args.browser_site,
            browser_channel=args.browser_channel,
            browser_slow_mo_ms=args.browser_slow_mo_ms,
            allowed_domains=[
                domain.strip()
                for domain in (args.allowed_domains or "").split(",")
                if domain.strip()
            ],
        )
    except ConfigError as exc:
        print(f"配置错误: {exc}")
        log_kv(logger, logging.ERROR, "Startup validation failed", error=str(exc))
        return 2

    print(task.report or "")
    return 0 if task.status == "success" else 1
