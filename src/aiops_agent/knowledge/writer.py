from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from aiops_agent.config import KnowledgeConfig, LLMProviderConfig


@dataclass(slots=True)
class KnowledgeWriteDraft:
    title: str
    aliases: list[str] = field(default_factory=list)
    system: str = "unknown"
    type: str = "runbooks"
    env: str = "prod"
    severity: str = "unknown"
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    body: str = ""
    related_links: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KnowledgeWriteResult:
    note_path: str = ""
    title: str = ""
    type: str = "runbooks"
    moc_path: str = ""
    updated_links: list[str] = field(default_factory=list)
    reindex_status: str = "not_started"
    missing_info: list[str] = field(default_factory=list)
    error: str | None = None
    dry_run: bool = False
    moc_updated: bool = False
    draft_summary: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing_info

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KnowledgeNoteWriter:
    VALID_TYPES = {"incident", "runbooks", "architecture", "guidance"}
    TYPE_TAGS = {
        "incident": "type/incident",
        "runbooks": "type/runbook",
        "architecture": "type/architecture",
        "guidance": "type/guidance",
    }
    KNOWN_COMPONENTS = ("weblogic", "nginx", "icip", "ukey", "堡垒机")

    def __init__(
        self,
        config: KnowledgeConfig,
        llm_config: LLMProviderConfig | None = None,
        *,
        engine: object | None = None,
    ):
        self.config = config
        self.llm_config = llm_config
        self.engine = engine
        self.vault = Path(config.vault_path) if config.vault_path else None

    def write(
        self,
        *,
        instruction: str,
        conversation_history: list[dict] | None = None,
        dry_run: bool = False,
        system: str | None = None,
        env: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> KnowledgeWriteResult:
        validation = self._validate_ready(dry_run=dry_run)
        if validation is not None:
            return validation

        history = conversation_history or []
        content_error = self._validate_content(instruction, history, dry_run=dry_run)
        if content_error is not None:
            return content_error

        draft = self._normalize_draft(
            self._draft_with_llm(instruction, history, default_system=system, default_env=env),
            fallback_instruction=instruction,
            default_system=system,
            default_env=env,
        )
        target_dir = self._target_dir(draft.type)
        note_title = self._note_title(draft.system, draft.title)
        note_path = target_dir / f"{self._safe_filename(note_title)}.md"
        path_error = self._validate_target(note_path)
        if path_error is not None:
            return path_error
        if note_path.exists():
            return KnowledgeWriteResult(
                note_path=str(note_path),
                title=note_title,
                type=draft.type,
                error=f"笔记已存在，不会覆盖：{note_path}。请显式说明要更新哪一篇笔记。",
                dry_run=dry_run,
                draft_summary=draft.summary,
            )

        moc_path = self._moc_path(target_dir, draft.type)
        related_links = self._clean_links(draft.related_links, exclude_stems={note_path.stem})
        markdown = self._render_markdown(note_title, draft, related_links)

        if dry_run:
            return KnowledgeWriteResult(
                note_path=str(note_path),
                title=note_title,
                type=draft.type,
                moc_path=str(moc_path),
                updated_links=related_links,
                reindex_status="dry_run",
                dry_run=True,
                draft_summary=draft.summary,
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        note_path.write_text(markdown, encoding="utf-8")
        moc_updated = self._update_moc(moc_path, note_path.stem, draft.summary)
        reindex_status = self._reindex()

        return KnowledgeWriteResult(
            note_path=str(note_path),
            title=note_title,
            type=draft.type,
            moc_path=str(moc_path),
            updated_links=related_links,
            reindex_status=reindex_status,
            dry_run=False,
            moc_updated=moc_updated,
            draft_summary=draft.summary,
        )

    def _validate_ready(self, *, dry_run: bool) -> KnowledgeWriteResult | None:
        if not self.config.write_enabled:
            return KnowledgeWriteResult(
                missing_info=["knowledge.write_enabled"],
                error="知识库写入未启用，请设置 knowledge.write_enabled=true。",
                dry_run=dry_run,
            )
        if not self.config.vault_path:
            return KnowledgeWriteResult(
                missing_info=["knowledge.vault_path"],
                error="Obsidian vault 尚未配置，请设置 knowledge.vault_path。",
                dry_run=dry_run,
            )
        if self.vault is None or not self.vault.exists() or not self.vault.is_dir():
            return KnowledgeWriteResult(
                missing_info=["valid knowledge.vault_path"],
                error="Obsidian vault 路径不存在或不是目录。",
                dry_run=dry_run,
            )
        if self.llm_config is None or not self.llm_config.enabled:
            return KnowledgeWriteResult(
                missing_info=["llm.enabled"],
                error="LLM 未启用，无法自动整理知识库主题笔记。",
                dry_run=dry_run,
            )
        return None

    def _validate_content(
        self,
        instruction: str,
        history: list[dict],
        *,
        dry_run: bool,
    ) -> KnowledgeWriteResult | None:
        direct_content = self._content_after_write_directive(instruction)
        asks_for_following_content = bool(re.search(r"(以下内容|下面内容|如下内容|如下|下列内容)", instruction))
        if direct_content:
            return None
        if history and not asks_for_following_content:
            return None
        return KnowledgeWriteResult(
            missing_info=["knowledge_write.content"],
            error="没有检测到需要写入知识库的内容。请在指令后粘贴内容，或先输入内容后使用 /save-note。",
            dry_run=dry_run,
        )

    def _draft_with_llm(
        self,
        instruction: str,
        history: list[dict],
        *,
        default_system: str | None,
        default_env: str | None,
    ) -> dict[str, Any]:
        history_text = "\n".join(
            f"Q: {turn.get('question', '')}\nA: {turn.get('answer', '')}"
            for turn in history[-5:]
        ) or "无"
        prompt = (
            "请把用户显式要求沉淀的内容和最近 QA 上下文整理为一篇 Obsidian 运维主题笔记草稿。\n"
            "只返回 JSON，不要输出 Markdown fence。\n"
            "Schema: {"
            "\"title\": str, \"aliases\": [str], \"system\": str, "
            "\"type\": \"incident|runbooks|architecture|guidance\", "
            "\"env\": str, \"severity\": str, \"tags\": [str], "
            "\"summary\": str, \"body\": str, \"related_links\": [str]"
            "}\n"
            "要求：title 是不含系统名前缀的短标题；body 使用 Markdown，包含背景、判断、处理步骤或结论；"
            "必须保留用户输入中的 SQL、Shell、配置片段和代码块，不要省略或改写原始命令；"
            "不要默认生成“## 原始资料”；只有 SQL、Shell、配置或代码块无法融入正文且否则会丢失时，才保留必要 fenced code block；"
            "如果正文已经结构化覆盖原始材料，不要重复粘贴原文；"
            "tags 如需输出，使用 system/系统名、type/runbook、component/weblogic、env/prod、severity/P2 这类 Obsidian 层级标签；"
            "related_links 只给你确信已存在的 Obsidian 笔记名，不确定存在时留空；不要包含 [[ ]]、!、markdown 链接或解释。\n"
            f"default_system: {default_system or 'unknown'}\n"
            f"default_env: {default_env or 'prod'}\n"
            f"instruction: {instruction}\n"
            f"recent_qa:\n{history_text}"
        )
        model = self._build_model()
        response = model.invoke(
            [
                SystemMessage(content="你是企业运维知识库编辑。严格返回 JSON。"),
                HumanMessage(content=prompt),
            ]
        )
        raw_text = self._message_text(response)
        try:
            parsed = json.loads(self._strip_json_fence(raw_text))
        except json.JSONDecodeError as exc:
            raise ValueError("LLM returned invalid JSON for knowledge note draft") from exc
        if not isinstance(parsed, dict):
            raise ValueError("LLM returned invalid knowledge note draft")
        return parsed

    def _normalize_draft(
        self,
        raw: dict[str, Any],
        *,
        fallback_instruction: str,
        default_system: str | None,
        default_env: str | None,
    ) -> KnowledgeWriteDraft:
        title = self._clean_scalar(raw.get("title")) or self._short_title(fallback_instruction)
        note_type = self._clean_scalar(raw.get("type")).lower()
        if note_type not in self.VALID_TYPES:
            note_type = "runbooks"
        system = self._infer_system(
            self._clean_scalar(raw.get("system")) or default_system or "unknown",
            fallback_instruction,
            raw,
        )
        env = self._clean_scalar(raw.get("env")) or default_env or "prod"
        severity = self._clean_scalar(raw.get("severity")) or "unknown"
        summary = self._clean_scalar(raw.get("summary")) or title
        body = str(raw.get("body") or "").strip()
        if not body:
            body = f"## 背景\n\n{fallback_instruction.strip() or title}\n\n## 处理记录\n\n待补充。"
        body = self._clean_redundant_original_sections(body)
        body = self._preserve_fenced_blocks(body, fallback_instruction)
        body = self._clean_redundant_original_sections(body)
        aliases = self._clean_list(raw.get("aliases"))
        if title not in aliases:
            aliases.insert(0, title)
        raw_tags = self._clean_list(raw.get("tags"))
        tags = self._canonical_tags(
            raw_tags=raw_tags,
            system=system,
            note_type=note_type,
            env=env,
            severity=severity,
            draft_text=" ".join(
                [
                    fallback_instruction,
                    title,
                    summary,
                    body,
                    " ".join(aliases),
                ]
            ),
        )
        return KnowledgeWriteDraft(
            title=title,
            aliases=aliases[:8],
            system=system,
            type=note_type,
            env=env,
            severity=severity,
            tags=tags[:12],
            summary=summary,
            body=body,
            related_links=self._clean_list(raw.get("related_links")),
        )

    def _target_dir(self, note_type: str) -> Path:
        assert self.vault is not None
        dir_name = self.config.note_type_dirs.get(note_type, self.config.note_type_dirs.get("runbooks", "runbooks"))
        dir_name = self._safe_relative_dir(dir_name or "runbooks")
        return self.vault / dir_name

    def _validate_target(self, note_path: Path) -> KnowledgeWriteResult | None:
        assert self.vault is not None
        try:
            vault = self.vault.resolve()
            parent = note_path.parent.resolve()
        except OSError:
            return KnowledgeWriteResult(note_path=str(note_path), error="无法解析笔记目标路径。")
        if parent != vault and vault not in parent.parents:
            return KnowledgeWriteResult(note_path=str(note_path), error="拒绝写入 vault 外部路径。")
        rel_path = note_path.relative_to(self.vault).as_posix()
        if self._is_excluded(rel_path):
            return KnowledgeWriteResult(note_path=str(note_path), error="拒绝写入知识库 exclude 目录。")
        return None

    def _is_excluded(self, rel_path: str) -> bool:
        return any(fnmatch.fnmatch(rel_path, pattern) for pattern in self.config.exclude_patterns)

    def _moc_path(self, target_dir: Path, note_type: str) -> Path:
        existing = sorted(target_dir.glob("*MOC.md")) if target_dir.exists() else []
        if existing:
            return existing[0]
        return target_dir / f"{note_type} MOC.md"

    def _render_markdown(self, note_title: str, draft: KnowledgeWriteDraft, related_links: list[str]) -> str:
        frontmatter = {
            "title": note_title,
            "aliases": draft.aliases,
            "system": draft.system,
            "type": draft.type,
            "env": draft.env,
            "severity": draft.severity,
            "tags": draft.tags,
            "last_updated": datetime.now(UTC).date().isoformat(),
        }
        fm_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        body = draft.body.strip()
        if related_links:
            related = "\n".join(f"- [[{link}]]" for link in related_links)
        else:
            related = "- 暂无"
        return f"---\n{fm_text}\n---\n\n# {note_title}\n\n{body}\n\n# 相关知识\n\n{related}\n"

    def _update_moc(self, moc_path: Path, note_stem: str, summary: str) -> bool:
        moc_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"- [[{note_stem}]]：{summary.strip() or note_stem}"
        if moc_path.exists():
            lines = moc_path.read_text(encoding="utf-8").splitlines()
        else:
            lines = [f"# {moc_path.stem}", ""]
        link_pattern = re.compile(rf"^\s*-\s*\[\[{re.escape(note_stem)}(?:\|[^\]]+)?\]\]")
        for index, existing in enumerate(lines):
            if link_pattern.search(existing):
                if existing == line:
                    return False
                lines[index] = line
                moc_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
                return True
        lines.append(line)
        moc_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return True

    def _reindex(self) -> str:
        if self.engine is None:
            return "skipped"
        try:
            reindex = getattr(self.engine, "reindex_after_write")
            return str(reindex())
        except Exception as exc:
            return f"failed: {exc}"

    def _build_model(self):
        assert self.llm_config is not None
        config = self.llm_config
        model_name = config.role_models.get("knowledge_write", config.role_models.get("knowledge", config.model))
        if config.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            kwargs: dict[str, Any] = {
                "model": model_name,
                "timeout": config.timeout_seconds,
                "max_retries": config.max_retries,
                "temperature": config.temperature,
                "max_tokens": max(config.max_tokens, 2048),
                "anthropic_api_key": config.api_key,
            }
            if config.base_url:
                kwargs["base_url"] = config.base_url
            if config.api_version:
                kwargs["default_headers"] = {"anthropic-version": config.api_version}
            return ChatAnthropic(**kwargs)
        if config.provider == "openai":
            from langchain_openai import ChatOpenAI
            kwargs = {
                "model": model_name,
                "timeout": config.timeout_seconds,
                "max_retries": config.max_retries,
                "temperature": config.temperature,
                "max_tokens": max(config.max_tokens, 2048),
                "api_key": config.api_key,
            }
            if config.base_url:
                kwargs["base_url"] = config.base_url
            return ChatOpenAI(**kwargs)
        raise ValueError(f"Unsupported LLM provider for knowledge write: {config.provider}")

    @staticmethod
    def _message_text(response: object) -> str:
        raw_text = getattr(response, "content", "")
        if isinstance(raw_text, list):
            fragments: list[str] = []
            for item in raw_text:
                if isinstance(item, dict) and item.get("type") == "text":
                    fragments.append(str(item.get("text", "")))
                elif hasattr(item, "text"):
                    fragments.append(str(getattr(item, "text")))
            raw_text = "".join(fragments)
        return str(raw_text).strip()

    @staticmethod
    def _strip_json_fence(raw_text: str) -> str:
        stripped = raw_text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            return "\n".join(lines).strip()
        return stripped

    @staticmethod
    def _clean_scalar(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = cls._clean_scalar(item)
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    @staticmethod
    def _short_title(text: str) -> str:
        cleaned = KnowledgeNoteWriter._content_after_write_directive(text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:，,。")
        return cleaned[:40] or "未命名笔记"

    @staticmethod
    def _note_title(system: str, short_title: str) -> str:
        system = system.strip() or "unknown"
        short_title = short_title.strip() or "未命名笔记"
        if short_title.lower().startswith(f"{system.lower()} - "):
            return short_title
        return f"{system} - {short_title}"

    @staticmethod
    def _safe_filename(value: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", value).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned[:120].rstrip(". ") or "未命名笔记"

    @staticmethod
    def _safe_relative_dir(value: str) -> str:
        cleaned = value.strip().strip("/\\")
        if not cleaned or cleaned.startswith(".") or ".." in Path(cleaned).parts or Path(cleaned).is_absolute():
            return "runbooks"
        return cleaned

    def _clean_links(self, values: list[str], *, exclude_stems: set[str] | None = None) -> list[str]:
        excluded = {item.lower() for item in (exclude_stems or set())}
        links: list[str] = []
        for value in values:
            target = value.strip()
            if target.startswith("!") or "[[" in target or "]]" in target:
                continue
            target = re.sub(r"\.md$", "", target, flags=re.IGNORECASE).strip()
            if not target or any(char in target for char in "\r\n[]|#"):
                continue
            if "://" in target:
                continue
            if target.startswith(("/", "\\")) or ".." in Path(target).parts:
                continue
            if target.lower() in excluded or Path(target).name.lower() in excluded:
                continue
            if not self._link_target_exists(target):
                continue
            if target not in links:
                links.append(target)
        return links[:10]

    def _link_target_exists(self, target: str) -> bool:
        existing = self._existing_note_link_targets()
        if target in existing:
            return True
        target_lower = target.lower()
        return any(item.lower() == target_lower for item in existing)

    def _existing_note_link_targets(self) -> set[str]:
        if self.vault is None or not self.vault.exists():
            return set()
        targets: set[str] = set()
        for path in self.vault.rglob("*.md"):
            if path.name.endswith("MOC.md"):
                continue
            try:
                rel_path = path.relative_to(self.vault).as_posix()
            except ValueError:
                continue
            if self._is_excluded(rel_path):
                continue
            rel_without_suffix = re.sub(r"\.md$", "", rel_path, flags=re.IGNORECASE)
            targets.add(rel_without_suffix)
            targets.add(path.stem)
        return targets

    @classmethod
    def _preserve_fenced_blocks(cls, body: str, source_text: str) -> str:
        blocks = cls._extract_fenced_blocks(source_text)
        if not blocks:
            return body
        missing: list[str] = []
        for block in blocks:
            if cls._body_contains_equivalent_fenced_block(body, block):
                continue
            missing.append(block.rstrip())
        if not missing:
            return body
        preserved = "\n\n".join(missing)
        if re.search(r"(?m)^#{2,6}\s*原始资料\s*$", body):
            return f"{body.rstrip()}\n\n{preserved}".strip()
        return f"{body.rstrip()}\n\n## 原始资料\n\n{preserved}".strip()

    @classmethod
    def _body_contains_equivalent_fenced_block(cls, body: str, source_block: str) -> bool:
        source_inner = cls._fenced_block_inner(source_block)
        if source_block.strip() in body or (source_inner and source_inner.strip() in body):
            return True
        return any(
            cls._fenced_blocks_equivalent(source_block, body_block)
            for body_block in cls._extract_fenced_blocks(body)
        )

    @classmethod
    def _clean_redundant_original_sections(cls, body: str) -> str:
        sections = list(cls._original_section_pattern().finditer(body))
        if not sections:
            return body
        non_original_body = cls._original_section_pattern().sub("", body)
        if not cls._has_structured_body(non_original_body):
            return cls._dedupe_original_sections(body)

        unique_blocks: list[str] = []
        for section in sections:
            for block in cls._extract_fenced_blocks(section.group(0)):
                if cls._body_contains_equivalent_fenced_block(non_original_body, block):
                    continue
                if any(cls._fenced_blocks_equivalent(block, existing) for existing in unique_blocks):
                    continue
                unique_blocks.append(block.rstrip())

        cleaned = re.sub(r"\n{3,}", "\n\n", non_original_body).strip()
        if not unique_blocks:
            return cleaned
        return f"{cleaned}\n\n## 原始资料\n\n" + "\n\n".join(unique_blocks)

    @classmethod
    def _has_structured_body(cls, body: str) -> bool:
        if cls._extract_fenced_blocks(body):
            return True
        text = re.sub(r"```[^\r\n]*(?:\r?\n).*?(?:\r?\n)```", "", body, flags=re.DOTALL)
        text = re.sub(r"(?m)^#{1,6}\s+.*$", "", text)
        compact_text = re.sub(r"\s+", "", text)
        heading_count = len(re.findall(r"(?m)^#{2,6}\s+\S+", body))
        list_count = len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)])\s+\S+", body))
        return heading_count >= 2 or list_count >= 2 or len(compact_text) >= 80

    @classmethod
    def _remove_redundant_original_sections(cls, body: str) -> str:
        sections = list(cls._original_section_pattern().finditer(body))
        if not sections:
            return body
        non_original_body = cls._original_section_pattern().sub("", body)
        remove_spans: list[tuple[int, int]] = []
        for section in sections:
            section_text = section.group(0)
            section_blocks = cls._extract_fenced_blocks(section_text)
            if not section_blocks or not cls._section_has_only_fenced_blocks(section_text):
                continue
            if all(cls._body_contains_equivalent_fenced_block(non_original_body, block) for block in section_blocks):
                remove_spans.append(section.span())
        return cls._remove_spans(body, remove_spans)

    @classmethod
    def _dedupe_original_sections(cls, body: str) -> str:
        sections = list(cls._original_section_pattern().finditer(body))
        if len(sections) <= 1:
            return body
        seen_blocks: list[str] = []
        remove_spans: list[tuple[int, int]] = []
        for section in sections:
            section_text = section.group(0)
            section_blocks = cls._extract_fenced_blocks(section_text)
            if not section_blocks or not cls._section_has_only_fenced_blocks(section_text):
                continue
            if all(
                any(cls._fenced_blocks_equivalent(block, seen_block) for seen_block in seen_blocks)
                for block in section_blocks
            ):
                remove_spans.append(section.span())
                continue
            seen_blocks.extend(section_blocks)
        return cls._remove_spans(body, remove_spans)

    @staticmethod
    def _original_section_pattern() -> re.Pattern[str]:
        return re.compile(r"(?ms)^#{2,6}\s*原始资料\s*\n.*?(?=^#{1,6}\s+|\Z)")

    @classmethod
    def _section_has_only_fenced_blocks(cls, section_text: str) -> bool:
        without_heading = re.sub(r"(?m)^#{2,6}\s*原始资料\s*\n?", "", section_text, count=1)
        without_blocks = re.sub(r"```[^\r\n]*(?:\r?\n).*?(?:\r?\n)```", "", without_heading, flags=re.DOTALL)
        return not without_blocks.strip()

    @staticmethod
    def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
        if not spans:
            return text
        chunks: list[str] = []
        cursor = 0
        for start, end in sorted(spans):
            chunks.append(text[cursor:start])
            cursor = end
        chunks.append(text[cursor:])
        return re.sub(r"\n{3,}", "\n\n", "".join(chunks)).strip()

    @classmethod
    def _fenced_blocks_equivalent(cls, left: str, right: str) -> bool:
        left_inner = cls._fenced_block_inner(left) if left.strip().startswith("```") else left.strip()
        right_inner = cls._fenced_block_inner(right) if right.strip().startswith("```") else right.strip()
        left_compact = cls._normalize_code_compact(left_inner)
        right_compact = cls._normalize_code_compact(right_inner)
        if not left_compact or not right_compact:
            return False
        shorter, longer = sorted((left_compact, right_compact), key=len)
        if len(shorter) >= 80 and shorter in longer and len(shorter) / len(longer) >= 0.75:
            return True

        left_tokens = set(cls._code_tokens(left_inner))
        right_tokens = set(cls._code_tokens(right_inner))
        if len(left_tokens) < 8 or len(right_tokens) < 8:
            return False
        overlap = left_tokens & right_tokens
        return len(overlap) / len(left_tokens) >= 0.82 and len(overlap) / len(right_tokens) >= 0.65

    @staticmethod
    def _normalize_code_compact(text: str) -> str:
        without_comments = re.sub(r"--[^\r\n]*", "", text.lower())
        return re.sub(r"[\s\[\];]+", "", without_comments)

    @staticmethod
    def _code_tokens(text: str) -> list[str]:
        without_comments = re.sub(r"--[^\r\n]*", "", text.lower())
        return re.findall(r"[a-z_][a-z0-9_$#]*|\d{4}-\d{2}-\d{2}|\d+|[<>]=?|=", without_comments)

    @staticmethod
    def _extract_fenced_blocks(text: str) -> list[str]:
        return [match.group(0) for match in re.finditer(r"```[^\r\n]*(?:\r?\n).*?(?:\r?\n)```", text, re.DOTALL)]

    @staticmethod
    def _fenced_block_inner(block: str) -> str:
        lines = block.strip().splitlines()
        if len(lines) <= 2:
            return ""
        if lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        return "\n".join(lines).strip()

    @staticmethod
    def _content_after_write_directive(text: str) -> str:
        cleaned = text.strip()
        cleaned = re.sub(
            r"(请|麻烦|帮我|把|将|以下内容|下面内容|如下内容|如下|下列内容)",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(记录到知识库|保存到知识库|添加入知识库|添加到知识库|加入知识库|写入知识库|录入知识库|整理到知识库|整理成知识库|生成知识库|知识沉淀|沉淀文档|写入\s*vault|写入vault|生成\s*knowledge|整理成\s*knowledge)",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"^[\s：:，,。#-]+", "", cleaned)
        cleaned = re.sub(r"[\s：:，,。]+$", "", cleaned)
        return cleaned.strip()

    def _canonical_tags(
        self,
        *,
        raw_tags: list[str],
        system: str,
        note_type: str,
        env: str,
        severity: str,
        draft_text: str,
    ) -> list[str]:
        tags: list[str] = []

        def add(tag: str) -> None:
            normalized = tag.strip().strip("#")
            if normalized and normalized not in tags:
                tags.append(normalized)

        if system:
            add(f"system/{system}")
        add(self.TYPE_TAGS.get(note_type, "type/runbook"))
        for component in self._infer_components(raw_tags, draft_text):
            add(f"component/{component}")
        if env:
            add(f"env/{env}")
        severity_value = severity.upper()
        if re.fullmatch(r"P[1-5]", severity_value):
            add(f"severity/{severity_value}")

        draft_text_lower = draft_text.lower()
        for tag in raw_tags:
            normalized = self._normalize_hierarchical_tag(tag)
            if normalized:
                if normalized.startswith("component/"):
                    component_value = normalized.split("/", 1)[1]
                    if component_value.lower() not in draft_text_lower:
                        continue
                add(normalized)
        return tags[:12]

    def _infer_components(self, raw_tags: list[str], draft_text: str) -> list[str]:
        haystack = draft_text.lower()
        components: list[str] = []
        for component in self.KNOWN_COMPONENTS:
            if component.lower() in haystack and component not in components:
                components.append(component)
        return components

    def _normalize_hierarchical_tag(self, tag: str) -> str | None:
        cleaned = tag.strip().strip("#")
        if not cleaned:
            return None
        lower = cleaned.lower()
        if lower == "type/runbooks":
            return "type/runbook"
        if lower in set(self.TYPE_TAGS.values()):
            return lower
        for prefix in ("system/", "component/", "env/", "severity/"):
            if lower.startswith(prefix):
                value = cleaned.split("/", 1)[1].strip()
                if not value:
                    return None
                if prefix == "component/":
                    for component in self.KNOWN_COMPONENTS:
                        if value.lower() == component.lower():
                            return f"component/{component}"
                if prefix == "severity/":
                    value = value.upper()
                return f"{prefix}{value}"
        if lower in self.KNOWN_COMPONENTS:
            component = next(item for item in self.KNOWN_COMPONENTS if item.lower() == lower)
            return f"component/{component}"
        return None

    def _infer_system(self, proposed: str, instruction: str, raw: dict[str, Any]) -> str:
        haystack = " ".join(
            [
                instruction,
                str(raw.get("title") or ""),
                str(raw.get("summary") or ""),
                str(raw.get("body") or ""),
            ]
        )
        if "财司" in haystack:
            return "财司系统"
        proposed = proposed.strip()
        if proposed.lower() == "weblogic" and "weblogic" not in haystack.lower():
            return "unknown"
        return proposed or "unknown"
