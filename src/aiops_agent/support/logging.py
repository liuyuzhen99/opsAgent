from __future__ import annotations

import logging
from typing import Any

from aiops_agent.support.trace import get_trace_id


class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = getattr(record, "trace_id", get_trace_id())
        return True


def configure_logging(level: str = "INFO") -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        for handler in root_logger.handlers:
            handler.addFilter(TraceIdFilter())
        root_logger.setLevel(level)
        return

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [trace_id=%(trace_id)s] %(name)s - %(message)s"
        )
    )
    handler.addFilter(TraceIdFilter())
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_kv(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    if fields:
        formatted = ", ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        logger.log(level, "%s | %s", message, formatted)
        return
    logger.log(level, message)
