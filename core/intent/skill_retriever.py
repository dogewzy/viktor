"""Project-scoped skill retrieval.

与 glossary/knowledge 检索同构：复用 BM25 + exact-match 提权，把 SkillItem 的
trigger_examples 当作主要语义信号，替代旧版 prompt_builder 里的子串/token 重叠打分。
"""

from __future__ import annotations

from core.intent.bm25 import BM25Index
from core.intent.tokenizer import normalize_text, tokenize
from core.registry import SkillItem, registry

# trigger_example 整句命中是强信号（用户问题正好落在沉淀的触发样例上），
# 量级对齐 glossary 里 term(+20)/alias(+14) 的 exact-match 提权。
_TRIGGER_EXACT_BOOST = 18.0
_NAME_EXACT_BOOST = 12.0


def retrieve_skills(
    project_id: str,
    query: str,
    *,
    limit: int = 8,
) -> list[tuple[SkillItem, float]]:
    """Return enabled skills ranked by BM25 over name/description/trigger_examples.

    分数 <= 0 的 skill 不返回（与 glossary_retriever 行为一致），让 prompt 层可以
    "命中才注入"。返回 (skill, score)，按分数降序、同分按 name 稳定排序。
    """
    try:
        items = registry.get_skills(project_id, only_enabled=True)
    except Exception:  # noqa: BLE001
        items = []
    if not items:
        return []

    docs = [
        tokenize(" ".join([
            item.name,
            item.description,
            " ".join(ex.text for ex in item.trigger_examples),
            " ".join(item.related_glossary_terms),
        ]))
        for item in items
    ]
    index = BM25Index(docs)
    query_tokens = tokenize(query)
    bm25_scores = index.scores(query_tokens)
    query_norm = normalize_text(query)

    scored: list[tuple[SkillItem, float]] = []
    for idx, item in enumerate(items):
        score = bm25_scores[idx]

        name_norm = normalize_text(item.name)
        if name_norm and name_norm in query_norm:
            score += _NAME_EXACT_BOOST

        for ex in item.trigger_examples:
            ex_norm = normalize_text(ex.text)
            if ex_norm and ex_norm in query_norm:
                score += _TRIGGER_EXACT_BOOST

        if score <= 0:
            continue
        scored.append((item, round(score, 4)))

    scored.sort(key=lambda pair: (pair[1], pair[0].name or pair[0].id), reverse=True)
    return scored[:limit]
