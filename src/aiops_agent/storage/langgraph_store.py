from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)


class FileBackedStore(BaseStore):
    """Small file-backed LangGraph Store for local development/runtime memory."""

    def __init__(self, root: str | Path = "storage/langgraph/store"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        results: list[Result] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self._get(op.namespace, op.key))
            elif isinstance(op, PutOp):
                self._put(op.namespace, op.key, op.value)
                results.append(None)
            elif isinstance(op, SearchOp):
                results.append(self._search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._list_namespaces(op))
            else:
                raise NotImplementedError(f"Unsupported LangGraph store op: {type(op).__name__}")
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await asyncio.to_thread(self.batch, list(ops))

    def _get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        record = self._read_record(namespace, key)
        if record is None:
            return None
        return Item(
            namespace=tuple(record["namespace"]),
            key=str(record["key"]),
            value=dict(record.get("value") or {}),
            created_at=self._parse_datetime(record.get("created_at")),
            updated_at=self._parse_datetime(record.get("updated_at")),
        )

    def _put(self, namespace: tuple[str, ...], key: str, value: dict[str, Any] | None) -> None:
        path = self._item_path(namespace, key)
        if value is None:
            if path.exists():
                path.unlink()
            return

        existing = self._read_record(namespace, key)
        now = datetime.now(UTC).isoformat()
        record = {
            "namespace": list(namespace),
            "key": str(key),
            "value": value,
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)

    def _search(self, op: SearchOp) -> list[SearchItem]:
        candidates: list[SearchItem] = []
        for record in self._iter_records():
            namespace = tuple(str(part) for part in record.get("namespace") or ())
            if not self._has_prefix(namespace, op.namespace_prefix):
                continue
            value = dict(record.get("value") or {})
            if not self._matches_filter(value, op.filter):
                continue
            score = self._query_score(value, op.query)
            if op.query and score <= 0:
                continue
            candidates.append(
                SearchItem(
                    namespace=namespace,
                    key=str(record.get("key") or ""),
                    value=value,
                    created_at=self._parse_datetime(record.get("created_at")),
                    updated_at=self._parse_datetime(record.get("updated_at")),
                    score=score if op.query else None,
                )
            )

        candidates.sort(
            key=lambda item: (
                item.score if item.score is not None else 0,
                item.updated_at,
                item.key,
            ),
            reverse=True,
        )
        start = max(0, int(op.offset))
        end = start + max(0, int(op.limit))
        return candidates[start:end]

    def _list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        namespaces = {tuple(str(part) for part in record.get("namespace") or ()) for record in self._iter_records()}
        filtered: set[tuple[str, ...]] = set()
        for namespace in namespaces:
            if not self._namespace_matches(namespace, op):
                continue
            if op.max_depth is not None:
                filtered.add(namespace[: max(0, int(op.max_depth))])
            else:
                filtered.add(namespace)
        ordered = sorted(filtered)
        start = max(0, int(op.offset))
        end = start + max(0, int(op.limit))
        return ordered[start:end]

    def _read_record(self, namespace: tuple[str, ...], key: str) -> dict[str, Any] | None:
        path = self._item_path(namespace, key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return record if isinstance(record, dict) else None

    def _iter_records(self) -> Iterable[dict[str, Any]]:
        for path in sorted(self.root.rglob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                yield record

    def _item_path(self, namespace: tuple[str, ...], key: str) -> Path:
        path = self.root
        for part in namespace:
            path /= self._encode(part)
        return path / f"{self._encode(key)}.json"

    def _encode(self, value: Any) -> str:
        raw = str(value).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") or "_"

    def _parse_datetime(self, value: Any) -> datetime:
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                pass
        return datetime.now(UTC)

    def _has_prefix(self, namespace: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
        return len(namespace) >= len(prefix) and namespace[: len(prefix)] == tuple(prefix)

    def _matches_filter(self, value: dict[str, Any], filter_value: dict[str, Any] | None) -> bool:
        if not filter_value:
            return True
        return all(value.get(key) == expected for key, expected in filter_value.items())

    def _query_score(self, value: dict[str, Any], query: str | None) -> float:
        if not query:
            return 0.0
        haystack = json.dumps(value, ensure_ascii=False).lower()
        tokens = [token for token in query.lower().split() if token]
        if not tokens:
            return 0.0
        matches = sum(1 for token in tokens if token in haystack)
        if query.lower() in haystack:
            matches += 2
        return float(matches)

    def _namespace_matches(self, namespace: tuple[str, ...], op: ListNamespacesOp) -> bool:
        for condition in op.match_conditions or ():
            path = tuple(str(part) for part in condition.path)
            if condition.match_type == "prefix" and not self._match_namespace_path(namespace, path, from_start=True):
                return False
            if condition.match_type == "suffix" and not self._match_namespace_path(namespace, path, from_start=False):
                return False
        return True

    def _match_namespace_path(self, namespace: tuple[str, ...], path: tuple[str, ...], *, from_start: bool) -> bool:
        if len(namespace) < len(path):
            return False
        selected = namespace[: len(path)] if from_start else namespace[-len(path) :]
        return all(expected == "*" or expected == actual for expected, actual in zip(path, selected, strict=False))
