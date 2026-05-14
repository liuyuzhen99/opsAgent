from __future__ import annotations

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
            return WebSkillMatch(skill=skill, score=score, parameters=parameters, actions=[], matched_keywords=matched_keywords)
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
