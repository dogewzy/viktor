"""Small multilingual tokenizer for project glossary retrieval."""

from __future__ import annotations

import re
from collections.abc import Iterable

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize_text(text: str) -> str:
    """Normalize text for retrieval without requiring external NLP packages."""
    return (text or "").replace("_", " ").replace("-", " ").lower()


def split_identifier(value: str) -> list[str]:
    """Split snake/camel/kebab identifiers into searchable lower-case pieces."""
    value = (value or "").replace("-", "_")
    pieces: list[str] = []
    for part in re.split(r"[_\s./:]+", value):
        for sub in _CAMEL_BOUNDARY_RE.sub(" ", part).split():
            item = sub.strip().lower()
            if item:
                pieces.append(item)
    return pieces


def cjk_ngrams(text: str, *, min_n: int = 2, max_n: int = 3) -> list[str]:
    out: list[str] = []
    for segment in _CJK_RE.findall(text or ""):
        if len(segment) < min_n:
            out.append(segment)
            continue
        for n in range(min_n, max_n + 1):
            if len(segment) >= n:
                out.extend(segment[i:i + n] for i in range(len(segment) - n + 1))
    return out


def tokenize(text: str) -> list[str]:
    """Tokenize Chinese business terms and English/code identifiers."""
    raw = text or ""
    tokens: list[str] = []
    tokens.extend(cjk_ngrams(raw))
    for word in _ASCII_WORD_RE.findall(raw):
        tokens.append(word.lower())
        tokens.extend(split_identifier(word))
    # Preserve compact code-ish fragments split by separators.
    for piece in re.split(r"\s+", normalize_text(raw)):
        if len(piece) >= 2:
            tokens.append(piece)
    return dedupe(tokens)


def dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = (item or "").strip().lower()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
