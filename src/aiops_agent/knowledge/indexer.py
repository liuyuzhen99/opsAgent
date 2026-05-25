from __future__ import annotations

import fnmatch
import re
import shutil
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

from rank_bm25 import BM25Okapi

from aiops_agent.config import KnowledgeConfig
from aiops_agent.knowledge.tokenizer import tokenize_knowledge_text


class VaultIndexer:
    CHUNK_SIZE = 800
    CHUNK_OVERLAP = 100
    MANIFEST_SCHEMA_VERSION = 4

    def __init__(self, config: KnowledgeConfig):
        self.config = config
        self.vault = Path(config.vault_path)

    def iter_docs(self) -> list[Document]:
        docs: list[Document] = []
        for path in sorted(self.vault.glob("**/*.md")):
            if not self._is_included(path) or self._is_excluded(path):
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            content, metadata = self._parse_frontmatter(raw, path)
            docs.append(Document(page_content=content, metadata=metadata))
        return docs

    def split_docs(self, docs: list[Document]) -> list[Document]:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.CHUNK_SIZE,
            chunk_overlap=self.CHUNK_OVERLAP,
        )
        chunks = splitter.split_documents(docs)
        counter: dict[str, int] = {}
        for chunk in chunks:
            src = chunk.metadata.get("source", "")
            chunk.metadata["chunk_index"] = counter.get(src, 0)
            counter[src] = counter.get(src, 0) + 1
        return chunks

    def build_keyword(self) -> tuple[BM25Okapi, list[Document]]:
        """Build BM25 index in memory (not persisted—safe and fast for typical vault sizes)."""
        chunks = self.split_docs(self.iter_docs())
        if not chunks:
            return BM25Okapi([[""]]), chunks
        tokenized = [self._tokenize(doc.page_content) for doc in chunks]
        return BM25Okapi(tokenized), chunks

    def expand_outlinks(self, docs: list[Document]) -> list[Document]:
        """Resolve existing vault notes linked by directly retrieved documents."""
        if not self.config.obsidian_graph_enabled or self.config.graph_expand_depth <= 0:
            return []

        note_lookup = self._build_note_lookup(self.iter_docs())
        seen_sources = {str(doc.metadata.get("source", "")) for doc in docs}
        expanded: list[Document] = []
        frontier = docs
        for _ in range(self.config.graph_expand_depth):
            next_frontier: list[Document] = []
            for parent in frontier:
                related_to = str(parent.metadata.get("title", parent.metadata.get("rel_path", "")))
                targets = str(parent.metadata.get("outlink_targets", "")).splitlines()
                for target in targets:
                    linked = note_lookup.get(self._normalize_link_target(target))
                    if linked is None:
                        continue
                    source = str(linked.metadata.get("source", ""))
                    if source in seen_sources:
                        continue
                    seen_sources.add(source)
                    metadata = dict(linked.metadata)
                    metadata["relation"] = "outlink"
                    metadata["related_to"] = related_to
                    resolved = Document(page_content=linked.page_content, metadata=metadata)
                    expanded.append(resolved)
                    next_frontier.append(resolved)
            frontier = next_frontier
            if not frontier:
                break
        return expanded

    def build_vector(self):
        """Build Chroma vector index persisted to vault/.chroma."""
        try:
            from langchain_community.vectorstores import Chroma
        except ImportError:
            from langchain_chroma import Chroma  # type: ignore[no-redef]

        chunks = self.split_docs(self.iter_docs())
        persist_path = self.vault / ".chroma"
        self._clear_vector_store(persist_path)
        persist_dir = str(persist_path)
        embeddings = self._make_embeddings()
        db = Chroma.from_documents(chunks, embeddings, persist_directory=persist_dir)
        self._write_manifest()
        return db

    def load_vector(self):
        """Load existing Chroma index from disk."""
        try:
            from langchain_community.vectorstores import Chroma
        except ImportError:
            from langchain_chroma import Chroma  # type: ignore[no-redef]

        persist_dir = str(self.vault / ".chroma")
        return Chroma(persist_directory=persist_dir, embedding_function=self._make_embeddings())

    def _make_embeddings(self):
        provider = (self.config.embedding_provider or "openai").lower()
        if provider == "huggingface":
            from langchain_huggingface import HuggingFaceEmbeddings
            return HuggingFaceEmbeddings(model_name=self.config.embedding_model)
        # default: openai-compatible
        from langchain_openai import OpenAIEmbeddings
        kwargs: dict = {
            "model": self.config.embedding_model,
            "api_key": self.config.embedding_api_key or None,
        }
        if self.config.embedding_base_url:
            kwargs["base_url"] = self.config.embedding_base_url
        return OpenAIEmbeddings(**kwargs)

    def is_vector_stale(self) -> bool:
        """Compare current index inputs against stored manifest."""
        manifest_path = self.vault / ".chroma" / "index_manifest.json"
        if not manifest_path.exists():
            return True

        import json
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return True

        if not isinstance(manifest, dict) or "schema_version" not in manifest:
            return True
        if manifest.get("schema_version") != self.MANIFEST_SCHEMA_VERSION:
            return True

        return manifest.get("files") != self._manifest_files() or manifest.get("index_options") != self._manifest_options()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_included(self, path: Path) -> bool:
        rel = path.relative_to(self.vault).as_posix()
        patterns = self.config.include_patterns or ["*.md"]
        return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)

    def _is_excluded(self, path: Path) -> bool:
        rel = path.relative_to(self.vault).as_posix()
        for pattern in self.config.exclude_patterns:
            if fnmatch.fnmatch(rel, pattern):
                return True
        return False

    def _parse_frontmatter(self, raw: str, path: Path) -> tuple[str, dict]:
        rel_path = path.relative_to(self.vault).as_posix()
        metadata: dict = {"source": str(path), "rel_path": rel_path}
        content = raw

        if raw.lstrip().startswith("```yaml\n---"):
            metadata["has_fenced_frontmatter"] = True

        if _YAML_AVAILABLE and raw.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---\n?(.*)", raw, re.DOTALL)
            if match:
                fm_text, body = match.group(1), match.group(2)
                try:
                    fm = yaml.safe_load(fm_text)
                    if isinstance(fm, dict):
                        self._apply_frontmatter(metadata, fm)
                except yaml.YAMLError:
                    pass
                content = body.strip()

        if "title" not in metadata:
            metadata["title"] = path.stem

        outlinks = self._parse_wikilinks(content) if self.config.obsidian_graph_enabled else []
        if outlinks:
            metadata["outlinks_text"] = " ".join(outlinks)
            metadata["outlink_targets"] = "\n".join(self._parse_wikilink_targets(content))
        if self._is_moc(rel_path):
            metadata["is_moc"] = True

        if self.config.link_context_enabled:
            content = self._append_link_context(content, metadata)

        return content, metadata

    def _apply_frontmatter(self, metadata: dict, fm: dict) -> None:
        key_map = {
            "title": "title",
            "tags": "tags",
            "aliases": "aliases",
            "type": "type",
            "类型": "type",
            "system": "system",
            "系统": "system",
            "env": "env",
            "环境": "env",
            "severity": "severity",
            "严重度": "severity",
            "component": "component",
            "组件": "component",
            "last_updated": "last_updated",
        }
        for source_key, target_key in key_map.items():
            if source_key in fm:
                metadata[target_key] = self._metadata_scalar(fm[source_key])

        if "aliases" in metadata:
            metadata["aliases_text"] = self._metadata_text(metadata["aliases"])
        if "tags" in metadata:
            metadata["tags_text"] = self._metadata_text(metadata["tags"])

    def _append_link_context(self, content: str, metadata: dict) -> str:
        lines: list[str] = []
        if metadata.get("title"):
            lines.append(f"相关标题：{metadata['title']}")
        if metadata.get("rel_path"):
            rel_path = str(metadata["rel_path"])
            note_name = Path(rel_path).stem
            lines.append(f"相关路径：{rel_path} {note_name}")
        if metadata.get("aliases_text"):
            lines.append(f"相关别名：{metadata['aliases_text']}")
        if metadata.get("tags_text"):
            lines.append(f"相关标签：{metadata['tags_text']}")
        attrs = [
            metadata.get("system"),
            metadata.get("type"),
            metadata.get("component"),
            metadata.get("env"),
            metadata.get("severity"),
        ]
        attrs_text = " ".join(str(value) for value in attrs if value)
        if attrs_text:
            lines.append(f"相关属性：{attrs_text}")
        if metadata.get("outlinks_text"):
            lines.append(f"相关链接：{metadata['outlinks_text']}")
        if not lines:
            return content
        return f"{content.rstrip()}\n\n知识库上下文：\n" + "\n".join(lines)

    def _is_moc(self, rel_path: str) -> bool:
        return any(fnmatch.fnmatch(rel_path, pattern) for pattern in self.config.moc_patterns)

    def _build_note_lookup(self, docs: list[Document]) -> dict[str, Document]:
        lookup: dict[str, Document] = {}
        for doc in docs:
            rel_path = str(doc.metadata.get("rel_path", ""))
            keys = [
                re.sub(r"\.md$", "", rel_path, flags=re.IGNORECASE),
                Path(rel_path).stem,
                str(doc.metadata.get("title", "")),
            ]
            for key in keys:
                normalized = self._normalize_link_target(key)
                if normalized:
                    lookup.setdefault(normalized, doc)
        return lookup

    @staticmethod
    def _normalize_link_target(target: str) -> str:
        normalized = target.strip().replace("\\", "/").split("#", 1)[0].strip()
        normalized = re.sub(r"\.md$", "", normalized, flags=re.IGNORECASE)
        return normalized.casefold()

    @staticmethod
    def _parse_wikilinks(content: str) -> list[str]:
        links: list[str] = []
        for match in re.finditer(r"(!?)\[\[([^\]]+)\]\]", content):
            if match.group(1):
                continue
            target_text = match.group(2).strip()
            if not target_text:
                continue
            target, _, alias = target_text.partition("|")
            target = target.strip()
            alias = alias.strip()
            if target:
                links.append(target)
            if alias:
                links.append(alias)
        return links

    @staticmethod
    def _parse_wikilink_targets(content: str) -> list[str]:
        targets: list[str] = []
        for match in re.finditer(r"(!?)\[\[([^\]]+)\]\]", content):
            if match.group(1):
                continue
            target = match.group(2).partition("|")[0].strip()
            if target and target not in targets:
                targets.append(target)
        return targets

    @staticmethod
    def _metadata_scalar(value):
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        if isinstance(value, dict):
            return " ".join(f"{key}:{item}" for key, item in value.items())
        return str(value)

    @staticmethod
    def _metadata_text(value) -> str:
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        if isinstance(value, dict):
            return " ".join(f"{key}:{item}" for key, item in value.items())
        return str(value)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return tokenize_knowledge_text(text)

    def _write_manifest(self) -> None:
        import json
        manifest = {
            "schema_version": self.MANIFEST_SCHEMA_VERSION,
            "files": self._manifest_files(),
            "index_options": self._manifest_options(),
        }
        manifest_path = self.vault / ".chroma" / "index_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _manifest_files(self) -> dict[str, float]:
        return {
            str(p): p.stat().st_mtime
            for p in self.vault.glob("**/*.md")
            if self._is_included(p) and not self._is_excluded(p)
        }

    def _manifest_options(self) -> dict:
        return {
            "link_context_enabled": self.config.link_context_enabled,
            "obsidian_graph_enabled": self.config.obsidian_graph_enabled,
            "moc_patterns": list(self.config.moc_patterns),
        }

    def _clear_vector_store(self, persist_path: Path) -> None:
        try:
            vault = self.vault.resolve()
            target = persist_path.resolve()
        except OSError:
            return
        if target == vault or vault not in target.parents:
            return
        if persist_path.exists():
            shutil.rmtree(persist_path)
