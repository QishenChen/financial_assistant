"""
Page resolver — maps text evidence to PDF page numbers.
Uses indices/page_map.json built by build_page_map.py.
Supports partial key matching (e.g. "annual_cmb_2025_report" matches "financial_reports/annual_cmb_2025_report.md").
"""

import json
import os
import re
import hashlib

PAGE_MAP_PATH = os.path.join("indices", "page_map.json")
_page_map_cache = None
_resolve_cache = {}

# Tuning constants
_NGRAM_SIZE = 4
_MIN_SNIPPET_LEN = 10
_MIN_SCORE_THRESHOLD = 0.5
_MIN_CONFIDENCE_GAP = 0.15


def _load_page_map() -> dict:
    global _page_map_cache
    if _page_map_cache is None:
        if os.path.exists(PAGE_MAP_PATH):
            with open(PAGE_MAP_PATH, "r", encoding="utf-8") as f:
                _page_map_cache = json.load(f)
        else:
            _page_map_cache = {"documents": {}}
    return _page_map_cache


def _normalize(text: str) -> str:
    """
    Normalize text for matching:
    - Lowercase ASCII characters.
    - Remove punctuation and symbols.
    - Collapse whitespace.
    Keeps Chinese characters, alphanumerics, and Hangul/Kana.
    """
    if not text:
        return ""
    # Lowercase ASCII only; leave CJK case as-is
    text = text.lower()
    # Strip punctuation/symbols but keep word characters and whitespace
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    # Collapse whitespace
    text = re.sub(r"\s+", "", text)
    return text


def _ngram_set(text: str, n: int = _NGRAM_SIZE) -> set[str]:
    """Return set of contiguous n-grams from text."""
    if len(text) < n:
        return set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _score_snippet_page(snippet_norm: str, page_norm: str) -> float:
    """
    Compute substring n-gram overlap between snippet and page.
    Score = (# of snippet n-grams found in page) / (# of snippet n-grams).
    """
    if not snippet_norm or not page_norm:
        return 0.0

    # For very short snippets, fall back to direct substring containment.
    if len(snippet_norm) < _NGRAM_SIZE:
        return 1.0 if snippet_norm in page_norm else 0.0

    snippet_ngrams = _ngram_set(snippet_norm, _NGRAM_SIZE)
    if not snippet_ngrams:
        return 0.0

    matched = sum(1 for ng in snippet_ngrams if ng in page_norm)
    return matched / len(snippet_ngrams)


def _find_doc_info(page_map: dict, doc_rel_path: str):
    """Locate document info, preferring exact match then partial match."""
    documents = page_map.get("documents", {})
    doc_info = documents.get(doc_rel_path)
    if doc_info and doc_info.get("total_pages", 0) > 0:
        return doc_info

    for key, info in documents.items():
        if doc_rel_path in key or key.endswith(doc_rel_path):
            if info.get("total_pages", 0) > 0:
                return info
    return None


def resolve_page(doc_rel_path: str, text_snippet: str) -> int | None:
    """
    Find the page number for a text snippet in a document.
    Uses n-gram overlap against per-page text snippets.
    Returns page number (1-based) or None when uncertain.
    """
    page_map = _load_page_map()
    doc_info = _find_doc_info(page_map, doc_rel_path)
    if not doc_info or doc_info.get("total_pages", 0) == 0:
        return None

    snippet_norm = _normalize(text_snippet[:300])
    if len(snippet_norm) < _MIN_SNIPPET_LEN:
        return None

    cache_key = (
        doc_info.get("rel_path", doc_rel_path),
        hashlib.md5(snippet_norm.encode("utf-8")).hexdigest(),
    )
    if cache_key in _resolve_cache:
        return _resolve_cache[cache_key]

    page_snippets = doc_info.get("page_snippets", {})
    if not page_snippets:
        return None

    best_page = None
    best_score = -1.0
    second_score = -1.0

    for page_str, page_text in page_snippets.items():
        page_norm = _normalize(page_text)
        if not page_norm:
            continue
        score = _score_snippet_page(snippet_norm, page_norm)

        if score > best_score:
            second_score = best_score
            best_score = score
            best_page = int(page_str)
        elif score > second_score:
            second_score = score

    # Reject if below threshold or if the runner-up is too close.
    if best_score < _MIN_SCORE_THRESHOLD:
        result = None
    elif best_score - second_score < _MIN_CONFIDENCE_GAP:
        result = None
    else:
        result = best_page

    _resolve_cache[cache_key] = result
    return result


def get_raw_pdf_path(doc_rel_path: str) -> str | None:
    """Get the raw PDF path for a document. Supports partial key matching."""
    page_map = _load_page_map()
    doc_info = page_map.get("documents", {}).get(doc_rel_path)

    # Try partial matching
    if not doc_info:
        for key, info in page_map.get("documents", {}).items():
            if doc_rel_path in key or key.endswith(doc_rel_path):
                doc_info = info
                break

    if not doc_info:
        return None
    raw_rel = doc_info.get("raw_rel_path", "")
    if raw_rel:
        if raw_rel.endswith('.pdf'):
            return raw_rel
        raw_rel += '.pdf'
        return raw_rel
    return None
