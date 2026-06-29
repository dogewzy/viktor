"""Project-scoped knowledge-note retrieval."""

from __future__ import annotations

from core.intent.bm25 import BM25Index
from core.intent.models import RetrievalHit
from core.intent.tokenizer import normalize_text, tokenize
from core.registry import registry

_KIND_BOOST = {
    "field_semantics": 6.0,
    "metric_definition": 5.0,
    "schema_convention": 3.0,
    "pitfall": 2.0,
}


def retrieve_knowledge_notes(project_id: str, query: str, *, limit: int = 8) -> list[RetrievalHit]:
    try:
        items = registry.get_knowledge_notes(project_id, only_enabled=True)
    except Exception:  # noqa: BLE001
        items = []
    if not items:
        return []

    docs = [
        tokenize(" ".join([
            item.kind,
            item.scope,
            item.title,
            item.content,
            " ".join(item.tags),
        ]))
        for item in items
    ]
    index = BM25Index(docs)
    query_tokens = tokenize(query)
    query_norm = normalize_text(query)
    hits: list[RetrievalHit] = []
    for idx, item in enumerate(items):
        score = index.score(query_tokens, idx)
        matched_fields: list[str] = []
        title_norm = normalize_text(item.title)
        scope_norm = normalize_text(item.scope)
        if title_norm and title_norm in query_norm:
            score += 8
            matched_fields.append("title")
        if scope_norm and scope_norm in query_norm:
            score += 4
            matched_fields.append("scope")
        if score <= 0:
            continue
        score += _KIND_BOOST.get(item.kind, 0.0)
        hits.append(RetrievalHit(
            id=item.id,
            title=item.title,
            kind=item.kind,
            score=round(score, 4),
            match_reason=";".join(matched_fields or ["bm25"]),
            matched_fields=matched_fields or ["bm25"],
            description=item.scope,
            content=item.content,
            metadata={"tags": list(item.tags), "source": item.source},
        ))
    hits.sort(key=lambda hit: (hit.score, hit.title), reverse=True)
    return hits[:limit]
