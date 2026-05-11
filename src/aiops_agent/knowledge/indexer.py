from __future__ import annotations

import fnmatch
import re
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


class VaultIndexer:
    CHUNK_SIZE = 800
    CHUNK_OVERLAP = 100

    def __init__(self, config: KnowledgeConfig):
        self.config = config
        self.vault = Path(config.vault_path)

    def iter_docs(self) -> list[Document]:
        docs: list[Document] = []
        for path in sorted(self.vault.glob("**/*.md")):
            if self._is_excluded(path):
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

    def build_vector(self):
        """Build Chroma vector index persisted to vault/.chroma."""
        try:
            from langchain_community.vectorstores import Chroma
        except ImportError:
            from langchain_chroma import Chroma  # type: ignore[no-redef]

        chunks = self.split_docs(self.iter_docs())
        persist_dir = str(self.vault / ".chroma")
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
        """Compare current .md mtimes against stored manifest; stale if any file changed."""
        manifest_path = self.vault / ".chroma" / "index_manifest.json"
        if not manifest_path.exists():
            return True

        import json
        try:
            manifest: dict[str, float] = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return True

        current: dict[str, float] = {
            str(p): p.stat().st_mtime
            for p in self.vault.glob("**/*.md")
            if not self._is_excluded(p)
        }
        return current != manifest

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_excluded(self, path: Path) -> bool:
        rel = path.relative_to(self.vault).as_posix()
        for pattern in self.config.exclude_patterns:
            if fnmatch.fnmatch(rel, pattern):
                return True
        return False

    def _parse_frontmatter(self, raw: str, path: Path) -> tuple[str, dict]:
        metadata: dict = {"source": str(path), "rel_path": path.relative_to(self.vault).as_posix()}
        content = raw

        if _YAML_AVAILABLE and raw.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---\n?(.*)", raw, re.DOTALL)
            if match:
                fm_text, body = match.group(1), match.group(2)
                try:
                    fm = yaml.safe_load(fm_text)
                    if isinstance(fm, dict):
                        for key in ("title", "tags", "system", "env", "severity", "last_updated"):
                            if key in fm:
                                metadata[key] = fm[key]
                except yaml.YAMLError:
                    pass
                content = body.strip()

        if "title" not in metadata:
            metadata["title"] = path.stem

        return content, metadata

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"\w+", text.lower())

    def _write_manifest(self) -> None:
        import json
        manifest = {
            str(p): p.stat().st_mtime
            for p in self.vault.glob("**/*.md")
            if not self._is_excluded(p)
        }
        manifest_path = self.vault / ".chroma" / "index_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
