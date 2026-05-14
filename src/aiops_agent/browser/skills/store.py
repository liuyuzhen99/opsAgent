from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from aiops_agent.browser.skills.models import WebSkill, WebSkillValidationError
from aiops_agent.browser.skills.validator import (
    parse_skill_markdown,
    validate_frontmatter,
    validate_skill_name,
    validate_workflow,
)


class WebSkillStore:
    def __init__(self, root: str | Path = "storage/web_skills"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> list[WebSkill]:
        skills: list[WebSkill] = []
        for skill_md in sorted(self.root.glob("*/SKILL.md")):
            try:
                skills.append(self.load(skill_md.parent.name))
            except WebSkillValidationError:
                continue
        return skills

    def load(self, name: str) -> WebSkill:
        skill_name = validate_skill_name(name)
        skill_root = self.root / skill_name
        skill_md = skill_root / "SKILL.md"
        workflow_path = skill_root / "assets" / "workflow.json"
        if not skill_md.exists():
            raise WebSkillValidationError(f"skill not found: {skill_name}")
        if not workflow_path.exists():
            raise WebSkillValidationError(f"workflow.json not found for skill: {skill_name}")
        frontmatter, body = parse_skill_markdown(skill_md.read_text(encoding="utf-8"))
        validate_frontmatter(frontmatter, skill_name)
        try:
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WebSkillValidationError(f"workflow.json is invalid for skill: {skill_name}") from exc
        validate_workflow(workflow, skill_name)
        return WebSkill(
            name=skill_name,
            description=str(frontmatter["description"]),
            root=skill_root,
            frontmatter=frontmatter,
            workflow=workflow,
            body=body,
        )

    def write(self, *, name: str, frontmatter: dict[str, Any], body: str, workflow: dict[str, Any], notes: str) -> Path:
        skill_name = validate_skill_name(name)
        validate_frontmatter(frontmatter, skill_name)
        validate_workflow(workflow, skill_name)
        skill_root = self.root / skill_name
        assets_dir = skill_root / "assets"
        references_dir = skill_root / "references"
        assets_dir.mkdir(parents=True, exist_ok=True)
        references_dir.mkdir(parents=True, exist_ok=True)

        skill_md = skill_root / "SKILL.md"
        frontmatter_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        skill_md.write_text(f"---\n{frontmatter_text}\n---\n\n{body.strip()}\n", encoding="utf-8")
        (assets_dir / "workflow.json").write_text(
            json.dumps(workflow, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (references_dir / "notes.md").write_text(notes.strip() + "\n", encoding="utf-8")
        return skill_root
