"""
Page resolver — maps text evidence to PDF page numbers.
Uses indices/page_map.json built by build_page_map.py.
Supports partial key matching (e.g., "annual_cmb_2025_report" matches "financial_reports/annual_cmb_2025_report.md").
"""

import json
import os
import re

PAGE_MAP_PATH = os.path.join("indices", "page_map.json")
_page_map_cache = None


def _load_page_map() -> dict:
    global _page_map_cache
    if _page_map_cache is None:
        if os.path.exists(PAGE_MAP_PATH):
            with open(PAGE_MAP_PATH, "r", encoding="utf-8") as f:
                _page_map_cache = json.load(f)
        else:
            _page_map_cache = {"documents": {}}
    return _page_map_cache


def resolve_page(doc_rel_path: str, text_snippet: str) -> int | None:
    """
    Find the page number for a text snippet in a document.
    Supports both exact and partial key matching.
    Returns page number (1-based) or None.
    """
    page_map = _load_page_map()
    documents = page_map.get("documents", {})
    
    # Try exact match first
    doc_info = documents.get(doc_rel_path)
    
    # Try partial matching if exact match fails
    if not doc_info or doc_info.get("total_pages", 0) == 0:
        for key, info in documents.items():
            if doc_rel_path in key or key.endswith(doc_rel_path):
                if info.get("total_pages", 0) > 0:
                    doc_info = info
                    break
    
    if not doc_info or doc_info.get("total_pages", 0) == 0:
        return None

    snippet_clean = re.sub(r'\s+', '', text_snippet[:100])
    if len(snippet_clean) < 5:
        return None

    # Search through page snippets for best match
    best_page = None
    best_score = 0
    for page_str, page_text in doc_info.get("page_snippets", {}).items():
        page_clean = re.sub(r'\s+', '', page_text)
        if not page_clean:
            continue
        matches = sum(1 for c in snippet_clean if c in page_clean)
        ratio = matches / max(len(snippet_clean), 1)
        if ratio > best_score:
            best_score = ratio
            best_page = int(page_str)

    return best_page if best_score > 0.3 else None


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