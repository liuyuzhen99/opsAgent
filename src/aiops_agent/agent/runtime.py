from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from aiops_agent.storage.langgraph_store import FileBackedStore


@dataclass(slots=True)
class LangGraphRuntimeConfig:
    checkpoint_path: Path = Path("storage/langgraph/checkpoints.sqlite")
    store_path: Path = Path("storage/langgraph/store")
    in_memory_checkpointer: bool = False
    in_memory_store: bool = False


@dataclass(slots=True)
class LangGraphRuntime:
    checkpointer: Any
    store: BaseStore
    checkpoint_backend: str
    store_backend: str
    checkpoint_path: Path | None = None
    store_path: Path | None = None
    _checkpointer_context: Any = None

    @classmethod
    def from_config(cls, config: LangGraphRuntimeConfig | None = None) -> "LangGraphRuntime":
        config = config or LangGraphRuntimeConfig()
        checkpointer, backend, context = _build_checkpointer(config)
        store, store_backend = _build_store(config)
        return cls(
            checkpointer=checkpointer,
            store=store,
            checkpoint_backend=backend,
            store_backend=store_backend,
            checkpoint_path=None if backend == "memory" else config.checkpoint_path,
            store_path=None if store_backend == "memory" else config.store_path,
            _checkpointer_context=context,
        )

    def close(self) -> None:
        context = self._checkpointer_context
        if context is not None and hasattr(context, "__exit__"):
            context.__exit__(None, None, None)
            self._checkpointer_context = None


def _build_checkpointer(config: LangGraphRuntimeConfig):
    if config.in_memory_checkpointer:
        return InMemorySaver(), "memory", None

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        return InMemorySaver(), "memory", None

    config.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver.from_conn_string(str(config.checkpoint_path))
    context = None
    if hasattr(saver, "__enter__"):
        context = saver
        saver = context.__enter__()
    if hasattr(saver, "setup"):
        saver.setup()
    return saver, "sqlite", context


def _build_store(config: LangGraphRuntimeConfig) -> tuple[BaseStore, str]:
    if config.in_memory_store:
        return InMemoryStore(), "memory"
    return FileBackedStore(config.store_path), "file"
