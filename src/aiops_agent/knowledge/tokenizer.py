from __future__ import annotations

import re


_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+")
_MAX_EXACT_CJK_TOKEN_LENGTH = 16


def tokenize_knowledge_text(text: str) -> list[str]:
    """Tokenize mixed Chinese/ASCII knowledge text for keyword retrieval."""
    tokens: list[str] = []
    for raw_token in _TOKEN_RE.findall(text.lower()):
        if _CJK_RE.match(raw_token):
            tokens.extend(_cjk_ngrams(raw_token))
        elif len(raw_token) >= 2:
            tokens.append(raw_token)
    return tokens


def _cjk_ngrams(token: str) -> list[str]:
    if len(token) <= 2:
        return [token]

    grams: list[str] = []
    if len(token) <= _MAX_EXACT_CJK_TOKEN_LENGTH:
        grams.append(token)
    grams.extend(token[i:i + 2] for i in range(len(token) - 1))
    if len(token) >= 3:
        grams.extend(token[i:i + 3] for i in range(len(token) - 2))
    return grams
