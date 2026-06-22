from __future__ import annotations

import json
import shutil
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

    def delete(self, name: str) -> Path:
        skill_name = validate_skill_name(name)
        skill_root = self.root / skill_name
        if not skill_root.exists():
            raise WebSkillValidationError(f"skill not found: {skill_name}")
        if not skill_root.is_dir():
            raise WebSkillValidationError(f"skill path is not a directory: {skill_name}")
        shutil.rmtree(skill_root)
        return skill_root

    def rename(self, old_name: str, new_name: str) -> Path:
        source_name = validate_skill_name(old_name)
        target_name = validate_skill_name(new_name)
        if source_name == target_name:
            raise WebSkillValidationError("new skill name must be different from the current name")

        skill = self.load(source_name)
        source_root = self.root / source_name
        target_root = self.root / target_name
        if target_root.exists():
            raise WebSkillValidationError(f"skill already exists: {target_name}")

        frontmatter = dict(skill.frontmatter)
        frontmatter["name"] = target_name
        workflow = json.loads(json.dumps(skill.workflow, ensure_ascii=False))
        workflow["skill_name"] = target_name
        validate_frontmatter(frontmatter, target_name)
        validate_workflow(workflow, target_name)

        try:
            shutil.copytree(source_root, target_root)
            frontmatter_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
            (target_root / "SKILL.md").write_text(
                f"---\n{frontmatter_text}\n---\n\n{skill.body.strip()}\n",
                encoding="utf-8",
            )
            (target_root / "assets" / "workflow.json").write_text(
                json.dumps(workflow, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.load(target_name)
        except Exception:
            if target_root.exists():
                shutil.rmtree(target_root)
            raise

        try:
            shutil.rmtree(source_root)
        except Exception:
            shutil.rmtree(target_root)
            raise
        return target_root
