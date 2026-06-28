"""
Shared index loader — a singleton that loads all indices once.
Replaces the old _retriever.py wrapper. Every tool calls this directly.
"""

import json
import os

INDICES_DIR = "indices"
HEADING_INDEX_PATH = os.path.join(INDICES_DIR, "heading_index.json")
TABLE_INDEX_PATH = os.path.join(INDICES_DIR, "table_index.json")
DOC_REGISTRY_PATH = os.path.join(INDICES_DIR, "doc_registry.json")
FINANCIAL_TERMS_PATH = "config/financial_terms.json"

_indices = None


def _load_indices():
    """Load all index files from disk. Called once lazily."""
    with open(HEADING_INDEX_PATH, "r", encoding="utf-8") as f:
        heading_index = json.load(f)
    with open(TABLE_INDEX_PATH, "r", encoding="utf-8") as f:
        table_index = json.load(f)
    with open(DOC_REGISTRY_PATH, "r", encoding="utf-8") as f:
        doc_registry = json.load(f)
    with open(FINANCIAL_TERMS_PATH, "r", encoding="utf-8") as f:
        financial_terms = json.load(f)
    return {
        "heading_index": heading_index,
        "table_index": table_index,
        "doc_registry": doc_registry,
        "financial_terms": financial_terms,
    }


def get_indices():
    """Return the cached index dict. Loads on first call."""
    global _indices
    if _indices is None:
        _indices = _load_indices()
    return _indices


def invalidate_indices_cache():
    """Force the next call to get_indices() to reload from disk.

    Call this after background indexing (e.g. upload_processor) so that
    Planner and search tools see newly extracted documents without restarting
    the server.
    """
    global _indices
    _indices = None