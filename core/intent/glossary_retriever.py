"""Project-scoped glossary retrieval."""

from __future__ import annotations

import re

from core.intent.bm25 import BM25Index
from core.intent.models import RetrievalHit
from core.intent.term_extractor import extract_query_terms
from core.intent.tokenizer import normalize_text, tokenize
from core.registry import registry

_IDENTIFIER_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{1,40}\b")


def retrieve_glossary(project_id: str, query: str, *, limit: int = 10) -> tuple[list[RetrievalHit], list[str]]:
    """Return top glossary hits and query terms that remain uncovered."""
    try:
        items = registry.get_glossaries(project_id, only_enabled=True)
    except Exception:  # noqa: BLE001
        items = []
    if not items:
        return [], _candidate_terms(query, covered_terms=[])

    docs = [
        tokenize(" ".join([
            item.term,
            " ".join(item.aliases),
            " ".join(item.code_keywords),
            item.description,
        ]))
        for item in items
    ]
    index = BM25Index(docs)
    query_tokens = tokenize(query)
    bm25_scores = index.scores(query_tokens)
    hits: list[RetrievalHit] = []
    query_norm = normalize_text(query)

    covered_terms: set[str] = set()
    for idx, item in enumerate(items):
        score = bm25_scores[idx]
        matched_fields: list[str] = []
        reason_parts: list[str] = []

        term_norm = normalize_text(item.term)
        if term_norm and term_norm in query_norm:
            score += 20
            matched_fields.append("term")
            reason_parts.append("exact_term")
            covered_terms.add(item.term)

        for alias in item.aliases:
            alias_norm = normalize_text(alias)
            if alias_norm and alias_norm in query_norm:
                score += 14
                matched_fields.append("aliases")
                reason_parts.append(f"alias:{alias}")
                covered_terms.add(alias)

        for keyword in item.code_keywords:
            keyword_norm = normalize_text(keyword)
            if keyword_norm and keyword_norm in query_norm:
                score += 10
                matched_fields.append("code_keywords")
                reason_parts.append(f"code:{keyword}")

        if score <= 0:
            continue

        matched_fields = sorted(set(matched_fields or ["bm25"]))
        hits.append(RetrievalHit(
            id=item.id,
            title=item.term,
            kind="glossary",
            score=round(score, 4),
            match_reason=", ".join(reason_parts) or "bm25",
            matched_fields=matched_fields,
            aliases=list(item.aliases),
            code_keywords=list(item.code_keywords),
            description=item.description,
            metadata={"project_id": item.project_id},
        ))

    hits.sort(key=lambda hit: (hit.score, hit.title), reverse=True)
    missing = _missing_terms(query, hits)
    return hits[:limit], missing


def _candidate_terms(query: str, *, covered_terms: list[str] | set[str]) -> list[str]:
    result = extract_query_terms(query)
    candidates = result.missing_terms(covered_terms)
    if candidates:
        return _dedupe(candidates)[:8]
    # Keep exact code-like identifiers as useful routing clues, but do not guess Chinese terms.
    return _dedupe(_IDENTIFIER_RE.findall(query or ""))[:8]


def _missing_terms(query: str, hits: list[RetrievalHit]) -> list[str]:
    covered = set()
    for hit in hits:
        covered.add(hit.title)
        covered.update(hit.aliases)
    missing: list[str] = []
    for term in _candidate_terms(query, covered_terms=covered):
        if not any(normalize_text(item) and normalize_text(item) in normalize_text(term) for item in covered):
            missing.append(term)
    return missing[:8]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        value = item.strip().lstrip("和与及")
        key = normalize_text(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out
