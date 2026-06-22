from __future__ import annotations

import re
from typing import Any

from aiops_agent.browser.skills.models import WebSkill, WebSkillMatch
from aiops_agent.browser.skills.renderer import WebSkillRenderer
from aiops_agent.browser.skills.store import WebSkillStore
from aiops_agent.browser.skills.validator import WebSkillValidationError


class WebSkillMatcher:
    def __init__(
        self,
        store: WebSkillStore | None = None,
        *,
        renderer: WebSkillRenderer | None = None,
        threshold: float = 0.75,
    ):
        self.store = store or WebSkillStore()
        self.renderer = renderer or WebSkillRenderer()
        self.threshold = threshold

    def match(self, goal: str, entities: dict[str, Any]) -> WebSkillMatch | None:
        best: WebSkillMatch | None = None
        for skill in self.store.list_skills():
            candidate = self._score_skill(skill, goal, entities)
            if candidate is None:
                continue
            if best is None or candidate.score > best.score:
                best = candidate
        if best is None or best.score < self.threshold:
            return None
        return best

    def match_by_name(self, skill_name: str, parameters: dict[str, str], entities: dict[str, Any]) -> WebSkillMatch:
        skill = self.store.load(skill_name)
        workflow = skill.workflow
        skill_site_key = str(workflow.get("site_key") or "")
        task_site_key = str(entities.get("site_key") or "")
        if skill_site_key and task_site_key and task_site_key != skill_site_key:
            raise WebSkillValidationError(f"skill site_key mismatch: {skill_site_key} != {task_site_key}")
        actions = self.renderer.render_actions(workflow, parameters, entities)
        return WebSkillMatch(
            skill=skill,
            score=1.0,
            parameters=parameters,
            actions=actions,
            matched_keywords=[skill_name],
        )

    def _score_skill(self, skill: WebSkill, goal: str, entities: dict[str, Any]) -> WebSkillMatch | None:
        workflow = skill.workflow
        skill_site_key = str(workflow.get("site_key") or "")
        task_site_key = str(entities.get("site_key") or "")
        if skill_site_key:
            if not task_site_key or task_site_key != skill_site_key:
                return None
            site_score = 0.4
        else:
            site_score = 0.2

        if not self._compound_structure_matches(workflow, goal, entities):
            return None

        explicit_navigation = self._explicit_navigation_labels(goal)
        if explicit_navigation and not self._workflow_supports_navigation(workflow, explicit_navigation):
            return None

        match_config = workflow.get("match") or {}
        keywords = [str(item) for item in match_config.get("keywords") or [] if str(item).strip()]
        fields = [str(item) for item in match_config.get("fields") or [] if str(item).strip()]
        haystack = goal.lower()
        matched_keywords = [keyword for keyword in keywords if keyword.lower() in haystack]
        matched_fields = [field for field in fields if field.lower() in haystack]
        keyword_score = 0.25 if matched_keywords else 0.0
        field_score = 0.15 if matched_fields else 0.0

        parameters = self.renderer.infer_parameters(workflow, goal, entities)
        required_inputs = [
            str(item.get("name"))
            for item in workflow.get("inputs") or []
            if isinstance(item, dict) and item.get("required", True)
        ]
        required_filled = [name for name in required_inputs if parameters.get(name)]
        if required_inputs:
            input_score = 0.2 * (len(required_filled) / len(required_inputs))
        else:
            input_score = 0.2

        score = min(site_score + keyword_score + field_score + input_score, 1.0)
        if required_inputs and len(required_filled) < len(required_inputs):
            return None
        try:
            actions = self.renderer.render_actions(workflow, parameters, entities)
        except WebSkillValidationError:
            return None
        return WebSkillMatch(
            skill=skill,
            score=score,
            parameters=parameters,
            actions=actions,
            matched_keywords=matched_keywords,
        )

    def _explicit_navigation_labels(self, goal: str) -> list[str]:
        labels: list[str] = []
        pattern = re.compile(
            r"依次点击\s*(.+?)(?=(?:进入对应菜单|进入[^,，。；;]{0,20}(?:[,，]|然后|之后|$)|"
            r"[,，]\s*(?:点击|等待|然后|之后|选中|选择|勾选|输入|填入|填写|告诉))|[。；;]|$)"
        )
        for match in pattern.finditer(goal):
            labels.extend(
                label
                for item in re.split(r"[,，、]", match.group(1))
                if (label := item.strip(" '\"“”"))
            )
        return labels

    def _workflow_supports_navigation(self, workflow: dict[str, Any], labels: list[str]) -> bool:
        configured_navigation = (workflow.get("match") or {}).get("navigation") or []
        if configured_navigation:
            click_targets = [self._normalize_navigation_label(str(label)) for label in configured_navigation]
        else:
            raw_actions = workflow.get("steps") or workflow.get("actions") or []
            click_targets = [
                self._normalize_navigation_label(str(action.get("target_hint") or ""))
                for action in raw_actions
                if isinstance(action, dict) and action.get("type") == "click" and action.get("target_hint")
            ]
        expected = [self._normalize_navigation_label(label) for label in labels]
        cursor = 0
        for target in click_targets:
            if cursor < len(expected) and target == expected[cursor]:
                cursor += 1
        return cursor == len(expected)

    def _normalize_navigation_label(self, value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()

    def _compound_structure_matches(self, workflow: dict[str, Any], goal: str, entities: dict[str, Any]) -> bool:
        goal_refs = [str(ref) for ref in (entities.get("credential_refs") or []) if str(ref).strip()]
        if not goal_refs:
            goal_refs = re.findall(
                r"(?:使用|用)\s*([A-Za-z0-9][A-Za-z0-9_.@:-]*)\s*(?:登录|登陆)",
                goal,
                flags=re.IGNORECASE,
            )
        execution = workflow.get("execution") or {}
        workflow_refs = [str(ref) for ref in (execution.get("credential_refs") or []) if str(ref).strip()]
        template = str(workflow.get("goal_template") or "")
        workflow_login_stages = re.findall(
            r"(?:使用|用)\s*[A-Za-z0-9][A-Za-z0-9_.@:-]*\s*(?:登录|登陆)",
            template,
            flags=re.IGNORECASE,
        )
        goal_stage_count = max(len(goal_refs), len(re.findall(r"依次点击", goal)))
        workflow_stage_count = max(len(workflow_refs), len(workflow_login_stages), len(re.findall(r"依次点击", template)))
        if goal_stage_count > 1 and workflow_stage_count < goal_stage_count:
            return False
        if workflow_stage_count > 1 and goal_stage_count < workflow_stage_count:
            return False
        if goal_refs and workflow_refs and goal_refs != workflow_refs:
            return False
        return True
