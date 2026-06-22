from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any


@dataclass(slots=True)
class TableRowCandidate:
    headers: list[str]
    values: list[str]
    row: dict[str, str]
    score: int
    row_start: int


class TextTableExtractor:
    _BROAD_OUTPUT_FIELDS = {"信息", "详情", "资料", "明细", "记录", "结果", "对应信息"}
    _HEADER_KEYWORDS = (
        "编号",
        "代码",
        "名称",
        "姓名",
        "账号",
        "账户",
        "用户",
        "客户",
        "公司",
        "单位",
        "部门",
        "机构",
        "角色",
        "岗位",
        "类型",
        "状态",
        "日期",
        "时间",
        "金额",
        "币种",
        "开户",
        "银行",
        "行号",
        "录入",
        "复核",
        "创建",
        "更新",
        "启用",
        "描述",
        "备注",
        "电话",
        "手机",
        "邮箱",
        "地址",
        "证件",
        "等级",
        "余额",
        "权限",
    )
    _CONTROL_TOKENS = {
        "查询",
        "搜索",
        "新增",
        "删除",
        "修改",
        "编辑",
        "保存",
        "确定",
        "取消",
        "关闭",
        "重置",
        "返回",
        "上一页",
        "下一页",
        "首页",
        "尾页",
        "更多",
        "操作",
        "查询条件",
        "Search",
        "Query",
        "Add",
        "Delete",
        "Modify",
        "Edit",
        "Save",
        "OK",
        "Cancel",
        "Close",
        "Reset",
        "Back",
        "MORE",
        "Page",
        "Displaying",
        "items",
    }
    _TITLE_SUFFIXES = ("管理", "列表", "页面", "菜单", "查询条件")
    _HEADER_ALIASES = {
        "用户编号": ("User No", "User ID", "User Number", "User Code"),
        "用户名称": ("User Name", "Username"),
        "登录名称": ("Login Name", "Login"),
        "所属单位": ("Subordinate Units", "Organization", "Org Name", "Unit"),
        "岗位名称": ("Duty Name", "Role Name", "Post Name"),
        "录入人": ("Input User Name", "Input User", "Created By", "Creator"),
        "录入日期": ("Input Date", "Input Time", "Create Date", "Create Time"),
        "修改人": ("Modify Name", "Modify User", "Modified By"),
        "修改日期": ("Modify Date", "Modify Time", "Modified Time"),
        "复核人": ("Check User Name", "Check User", "Reviewer"),
        "复核日期": ("Check Time", "Check Date", "Review Time"),
        "活动状态": ("Status", "Activity Status"),
        "复核状态": ("Check State", "Review Status"),
        "账户编号": ("Account No", "Account ID", "Account Number"),
        "账户名称": ("Account Name",),
        "客户名称": ("Customer Name",),
        "公司名称": ("Company Name",),
    }
    _HEADER_DISPLAY_NAMES = {
        alias: canonical
        for canonical, aliases in _HEADER_ALIASES.items()
        for alias in aliases
    }
    _ENGLISH_HEADER_PHRASES = tuple(
        sorted(_HEADER_DISPLAY_NAMES, key=lambda phrase: len(phrase.split()), reverse=True)
    )
    _ENGLISH_VALUE_PHRASES = (
        "Has been reviewed",
        "Has not been reviewed",
        "Not reviewed",
        "Activity in",
        "Inactive",
    )
    _VALUE_DISPLAY_NAMES = {
        "Activity in": "活动中",
        "Has been reviewed": "已复核",
        "Has not been reviewed": "未复核",
        "Not reviewed": "未复核",
        "Inactive": "未启用",
    }
    _PAGE_MARKER_RE = re.compile(r"(?:显示\s*\d+\s*到\s*\d+|显示\d+到\d+|共\s*\d+\s*记录|第\s*共\d+页)")
    _DATE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
    _CODE_RE = re.compile(r"[A-Za-z]{1,8}\d{2,}[A-Za-z0-9_.-]*")

    def detail_answer(self, goal: str, page_text: str, *, contract: dict[str, str] | None = None) -> dict[str, Any] | None:
        output_field = (contract or {}).get("output_field") or self._requested_output_field(goal) or ""
        if not self.is_broad_output_field(output_field):
            return None
        query_value = (contract or {}).get("query_value") or self._query_value_from_goal(goal)
        candidates = self.find_rows(page_text, query_value=query_value)
        if not candidates:
            return None
        best_score = max(candidate.score for candidate in candidates)
        candidates = [candidate for candidate in candidates if candidate.score >= best_score - 4]
        rows = self._dedupe_rows([candidate.row for candidate in candidates])
        answer = self._format_detail_answer(rows, query_value)
        return {"answer": answer, "rows": rows, "query_value": query_value, "answer_type": "table_detail"}

    def column_matches(self, page_text: str, *, query_field: str, query_value: str, output_field: str) -> list[dict[str, str]]:
        candidates = self.find_rows(page_text, query_value=query_value)
        matches: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for candidate in candidates:
            query_header = self._best_header(candidate.row, query_field, fallback_value=query_value)
            output_header = self._best_header(candidate.row, output_field)
            if not query_header or not output_header:
                continue
            candidate_query = candidate.row.get(query_header, "")
            output_value = candidate.row.get(output_header, "")
            if not candidate_query or not output_value:
                continue
            if query_value not in candidate_query and candidate_query not in query_value:
                continue
            row_id = self._row_identifier(candidate.row)
            key = (candidate_query, output_header, output_value)
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                {
                    "row_id": row_id,
                    "query_field": query_header,
                    "query_value": candidate_query,
                    "output_field": output_header,
                    "output_value": output_value,
                }
            )
        return matches

    def find_rows(self, page_text: str, *, query_value: str = "") -> list[TableRowCandidate]:
        tokens = self._tokens(page_text)
        if len(tokens) < 4:
            return []
        if query_value:
            candidates = self._row_candidates_for_query(tokens, query_value)
        else:
            candidates = self._first_row_candidates(tokens)
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        candidates = self._dedupe_candidates(candidates)
        if not candidates:
            return []
        best_headers = candidates[0].headers
        return sorted(
            [candidate for candidate in candidates if candidate.headers == best_headers],
            key=lambda candidate: candidate.row_start,
        )

    def is_broad_output_field(self, output_field: str) -> bool:
        field = output_field.strip("'\"“” ")
        if not field:
            return False
        if field in self._BROAD_OUTPUT_FIELDS:
            return True
        return bool(re.fullmatch(r"[\w\u4e00-\u9fff-]{1,12}(?:信息|详情|资料|明细|记录)", field))

    def _row_candidates_for_query(self, tokens: list[str], query_value: str) -> list[TableRowCandidate]:
        query_indexes = [
            index for index, token in enumerate(tokens)
            if query_value == token or query_value in token
        ]
        candidates: list[TableRowCandidate] = []
        for query_index in query_indexes:
            for width in range(2, min(18, len(tokens)) + 1):
                for query_offset in range(width):
                    row_start = query_index - query_offset
                    if row_start < 0 or query_index >= row_start + width:
                        continue
                    values = tokens[row_start:row_start + width]
                    if len(values) < width or self._contains_table_stop(values):
                        continue
                    candidates.extend(self._candidates_for_row(tokens, row_start, width, values, query_value))
        return candidates

    def _first_row_candidates(self, tokens: list[str]) -> list[TableRowCandidate]:
        candidates: list[TableRowCandidate] = []
        for width in range(2, min(18, len(tokens)) + 1):
            for row_start in range(width, min(len(tokens) - width + 1, 80)):
                values = tokens[row_start:row_start + width]
                if self._contains_table_stop(values):
                    continue
                candidates.extend(self._candidates_for_row(tokens, row_start, width, values, ""))
        return candidates

    def _candidates_for_row(
        self,
        tokens: list[str],
        row_start: int,
        width: int,
        values: list[str],
        query_value: str,
    ) -> list[TableRowCandidate]:
        candidates: list[TableRowCandidate] = []
        header_end = row_start
        for prior_rows in range(4):
            for header_width in range(width, min(width + 3, header_end) + 1):
                header_start = header_end - header_width
                if header_start < 0:
                    continue
                headers = tokens[header_start:header_end]
                if self._valid_headers(headers):
                    for row, aligned_values in self._aligned_rows(headers, values):
                        candidates.append(
                            TableRowCandidate(
                                headers=headers,
                                values=aligned_values,
                                row=row,
                                score=(
                                    self._candidate_score(headers, aligned_values, query_value, prior_rows)
                                    + self._alignment_score(row)
                                ),
                                row_start=row_start,
                            )
                    )
            header_end -= width
        return candidates

    def _aligned_rows(self, headers: list[str], values: list[str]) -> list[tuple[dict[str, str], list[str]]]:
        if len(headers) == len(values):
            return [(dict(zip(headers, values, strict=False)), values)]
        missing_count = len(headers) - len(values)
        if missing_count <= 0 or missing_count > 2:
            return []
        rows: list[tuple[dict[str, str], list[str]]] = []
        for missing_indexes in combinations(range(len(headers)), missing_count):
            missing = set(missing_indexes)
            aligned_values: list[str] = []
            value_index = 0
            for index in range(len(headers)):
                if index in missing:
                    aligned_values.append("")
                    continue
                aligned_values.append(values[value_index])
                value_index += 1
            rows.append((dict(zip(headers, aligned_values, strict=False)), aligned_values))
        return rows

    def _valid_headers(self, headers: list[str]) -> bool:
        if len(headers) < 2 or len(set(headers)) != len(headers):
            return False
        if any(self._is_control_or_title_token(token) for token in headers):
            return False
        if any(self._looks_like_data_token(token) for token in headers):
            return False
        label_count = sum(1 for token in headers if self._looks_like_header_token(token))
        return label_count >= max(2, len(headers) - 1)

    def _looks_like_header_token(self, token: str) -> bool:
        if token in self._HEADER_DISPLAY_NAMES:
            return True
        if not token or len(token) > 32:
            return False
        if self._DATE_RE.fullmatch(token):
            return False
        if self._CODE_RE.fullmatch(token) and not self._short_english_header(token):
            return False
        if token in {"ID", "Id", "id", "No", "NO", "Name", "Status", "Type", "Date"}:
            return True
        return any(keyword in token for keyword in self._HEADER_KEYWORDS)

    def _is_control_or_title_token(self, token: str) -> bool:
        if token in self._CONTROL_TOKENS:
            return True
        return any(token.endswith(suffix) for suffix in self._TITLE_SUFFIXES)

    def _contains_table_stop(self, values: list[str]) -> bool:
        return any(value in self._CONTROL_TOKENS or self._PAGE_MARKER_RE.search(value) for value in values)

    def _looks_like_data_token(self, token: str) -> bool:
        if self._DATE_RE.fullmatch(token):
            return True
        if self._CODE_RE.fullmatch(token) and not self._short_english_header(token):
            return True
        if "_" in token or "@" in token:
            return True
        if re.fullmatch(r"\d+(?:[.,]\d+)?", token):
            return True
        if re.fullmatch(r"[A-Za-z0-9_.-]{3,}", token) and not self._short_english_header(token):
            return True
        if "-" in token and not any(keyword in token for keyword in self._HEADER_KEYWORDS):
            return True
        return False

    def _candidate_score(self, headers: list[str], values: list[str], query_value: str, prior_rows: int) -> int:
        exact_query = 80 if query_value and any(value == query_value for value in values) else 0
        fuzzy_query = 30 if query_value and exact_query == 0 and any(query_value in value for value in values) else 0
        header_score = sum(4 for header in headers if self._looks_like_header_token(header))
        id_bonus = 8 if headers and any(keyword in headers[0] for keyword in ("编号", "代码", "ID", "No")) else 0
        return exact_query + fuzzy_query + header_score + id_bonus + len(headers) - prior_rows * 3

    def _alignment_score(self, row: dict[str, str]) -> int:
        score = 0
        for header, value in row.items():
            header_norm = header.lower()
            if not value:
                if any(token in header for token in ("岗位", "角色", "Duty", "Role", "Post")):
                    score += 6
                else:
                    score -= 2
                continue
            if any(token in header for token in ("编号", "代码", "ID", "No", "Code", "Number")):
                score += 8 if self._CODE_RE.fullmatch(value) else -4
            if any(token in header for token in ("日期", "时间", "Date", "Time")):
                score += 8 if self._DATE_RE.fullmatch(value) else -6
            if any(token in header for token in ("状态", "Status", "State")):
                score += 5 if not re.fullmatch(r"\d+", value) else -6
            if any(token in header for token in ("单位", "机构", "部门", "Unit", "Org")):
                score += 4 if ("_" in value or re.search(r"[\u4e00-\u9fff]", value)) else 0
            if "name" in header_norm and self._DATE_RE.fullmatch(value):
                score -= 4
        return score

    def _dedupe_candidates(self, candidates: list[TableRowCandidate]) -> list[TableRowCandidate]:
        deduped: list[TableRowCandidate] = []
        seen: set[tuple[tuple[str, str], ...]] = set()
        seen_slots: set[tuple[tuple[str, ...], int]] = set()
        for candidate in candidates:
            slot_key = (tuple(candidate.headers), candidate.row_start)
            if slot_key in seen_slots:
                continue
            seen_slots.add(slot_key)
            key = tuple(candidate.row.items())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    def _dedupe_rows(self, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        deduped: list[dict[str, str]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()
        for row in rows:
            key = tuple(row.items())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped

    def _best_header(self, row: dict[str, str], requested_field: str, *, fallback_value: str = "") -> str | None:
        headers = list(row)
        scored = [
            (self._header_match_score(header, requested_field), header)
            for header in headers
        ]
        scored = [(score, header) for score, header in scored if score > 0]
        if scored:
            return max(scored, key=lambda item: item[0])[1]
        if fallback_value:
            for header, value in row.items():
                if fallback_value == value or fallback_value in value:
                    return header
        return None

    def _header_match_score(self, header: str, requested_field: str) -> int:
        if header == requested_field:
            return 100
        header_aliases = self._header_alias_norms(header)
        requested_aliases = self._header_alias_norms(requested_field)
        if header_aliases and requested_aliases and header_aliases & requested_aliases:
            return 85
        header_norm = self._normalize_header(header)
        requested_norm = self._normalize_header(requested_field)
        if not header_norm or not requested_norm:
            return 0
        if header_norm == requested_norm:
            return 90
        if header in requested_field or requested_field in header:
            return 60
        if header_norm in requested_norm or requested_norm in header_norm:
            return 40
        return 0

    def _normalize_header(self, value: str) -> str:
        return re.sub(r"(字段|信息|详情|资料|名称|名)$", "", value.strip().replace(" ", "").lower())

    def _header_alias_norms(self, field: str) -> set[str]:
        aliases = {field}
        display_name = self._HEADER_DISPLAY_NAMES.get(field)
        if display_name:
            aliases.add(display_name)
            aliases.update(self._HEADER_ALIASES.get(display_name, ()))
        aliases.update(self._HEADER_ALIASES.get(field, ()))
        for canonical, values in self._HEADER_ALIASES.items():
            if field in values:
                aliases.add(canonical)
                aliases.update(values)
                break
        return {self._normalize_header(alias) for alias in aliases if alias}

    def _row_identifier(self, row: dict[str, str]) -> str:
        for header, value in row.items():
            if any(keyword in header for keyword in ("编号", "代码", "ID", "No")) and value:
                return value
        first = next(iter(row.values()), "")
        return first

    def _format_detail_answer(self, rows: list[dict[str, str]], query_value: str) -> str:
        answers: list[str] = []
        for index, row in enumerate(rows, start=1):
            subject = query_value or self._row_identifier(row) or f"匹配记录{index}"
            parts = [
                f"{self._display_header(field, value)}{self._display_value(value)}"
                for field, value in row.items()
                if value
            ]
            answers.append(f"{subject}的信息：" + "，".join(parts))
        return "；".join(answers) + "。"

    def _display_header(self, header: str, value: str = "") -> str:
        if header == "Modify Name" and self._DATE_RE.fullmatch(value):
            return "录入日期"
        return self._HEADER_DISPLAY_NAMES.get(header, header)

    def _display_value(self, value: str) -> str:
        return self._VALUE_DISPLAY_NAMES.get(value, value)

    def _query_value_from_goal(self, goal: str) -> str:
        match = re.search(
            r"查询[^,，。；;\s]*?([A-Za-z0-9_.@-]{2,}|[\u4e00-\u9fff]{2,10})(?:的)?(?:信息|详情|资料|明细|记录)",
            goal,
        )
        if match:
            return match.group(1).strip("'\"“”")
        return ""

    def _requested_output_field(self, goal: str) -> str | None:
        match = re.search(r"对应的\s*([^,，。；;\s]{2,20})", goal)
        if match:
            return match.group(1).strip("'\"“”")
        match = re.search(r"(?:告诉我|返回|输出)\s*([^,，。；;\s]{2,20})(?:$|[，。；,;])", goal)
        if match:
            return match.group(1).strip("'\"“”")
        return None

    def _tokens(self, page_text: str) -> list[str]:
        text = re.sub(r"\s+", " ", page_text).strip()
        tokens = [token for token in text.split(" ") if token]
        phrases = (*self._ENGLISH_HEADER_PHRASES, *self._ENGLISH_VALUE_PHRASES)
        return self._merge_phrases(tokens, phrases)

    def _merge_phrases(self, tokens: list[str], phrases: tuple[str, ...]) -> list[str]:
        phrase_tokens = [(phrase, phrase.lower().split()) for phrase in phrases]
        merged: list[str] = []
        index = 0
        while index < len(tokens):
            match: str | None = None
            match_len = 0
            for phrase, parts in phrase_tokens:
                end = index + len(parts)
                if end <= len(tokens) and [token.lower() for token in tokens[index:end]] == parts:
                    match = phrase
                    match_len = len(parts)
                    break
            if match is None:
                merged.append(tokens[index])
                index += 1
                continue
            merged.append(match)
            index += match_len
        return merged

    def _short_english_header(self, token: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z]{1,8}", token))
