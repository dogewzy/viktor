"""Small LLM-backed business term extractor for intent routing."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, Field

from core.intent.tokenizer import normalize_text
from core.llm_client import create_llm


TermKind = Literal["business_term", "metric_word", "colloquial_noise"]


class ExtractedTerm(BaseModel):
    text: str
    kind: TermKind = "business_term"
    confidence: float = 0.0
    reason: str = ""


class TermExtractionResult(BaseModel):
    terms: list[ExtractedTerm] = Field(default_factory=list)

    def missing_terms(self, covered_terms: list[str] | set[str] | None = None) -> list[str]:
        covered_norm = [normalize_text(term) for term in (covered_terms or []) if normalize_text(term)]
        out: list[str] = []
        seen: set[str] = set()
        for item in self.terms:
            text = item.text.strip()
            key = normalize_text(text)
            if not text or not key or key in seen:
                continue
            if item.kind != "business_term" or item.confidence < 0.45:
                continue
            if any(covered and (covered in key or key in covered) for covered in covered_norm):
                continue
            seen.add(key)
            out.append(text)
        return out[:8]


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)


def extract_query_terms(query: str) -> TermExtractionResult:
    """Extract business terms conservatively; on LLM failure, return no terms."""
    if not (query or "").strip():
        return TermExtractionResult()

    prompt = f"""
请从用户问题中抽取术语，帮助判断哪些业务词还没有被项目 Glossary 覆盖。

要求：
1. 只抽取用户真正想表达的业务实体/业务分类/系统内黑话。
2. 把「帮我」「看一下」「查一下」这类口语填充标为 colloquial_noise。
3. 把「比例」「数量」「分布」「统计」这类指标/动作词标为 metric_word。
4. 不要把完整句子当术语；每个 text 尽量 2-8 个中文字或一个英文标识符。
5. 不确定时降低 confidence；不要硬猜。
6. 只输出 JSON object，不要 Markdown。

输出格式：
{{
  "terms": [
    {{"text": "漫剧", "kind": "business_term", "confidence": 0.92, "reason": "业务分类词"}},
    {{"text": "比例", "kind": "metric_word", "confidence": 0.86, "reason": "统计指标词"}}
  ]
}}

用户问题：
{query}
""".strip()
    try:
        llm = create_llm(thinking=False, feature="intent_term_extraction")
        response = llm.invoke([
            SystemMessage(content="你是严格的业务术语抽取器，只输出可解析 JSON。"),
            HumanMessage(content=prompt),
        ])
        content = response.content if isinstance(response.content, str) else str(response.content or "")
        return _coerce_result(_extract_json(content))
    except Exception as e:  # noqa: BLE001
        logger.warning("intent term extraction failed, return empty missing_terms: {}", e)
        return TermExtractionResult()


def _extract_json(content: str) -> dict[str, Any]:
    match = _JSON_BLOCK_RE.search(content or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _coerce_result(data: dict[str, Any]) -> TermExtractionResult:
    terms: list[ExtractedTerm] = []
    for raw in data.get("terms") or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip(" ：:？?()（）[]【】\"'")
        if not text:
            continue
        kind = raw.get("kind") if raw.get("kind") in {"business_term", "metric_word", "colloquial_noise"} else "business_term"
        try:
            confidence = float(raw.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        terms.append(ExtractedTerm(
            text=text,
            kind=kind,
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(raw.get("reason") or "")[:200],
        ))
    return TermExtractionResult(terms=terms[:24])
