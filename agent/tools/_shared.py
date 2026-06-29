"""
Shared helpers extracted from retriever.py — used by all search tools.
"""
# (no external deps needed for these pure helpers)
import os

# Minimum fuzzy match score to consider a table/heading relevant
MIN_SCORE = 0.25

# Directory where extracted markdown files for the active corpus live.
# In uploaded-only mode this points to uploads/extracted/uploaded.
EXTRACTED_DIR = "uploads/extracted/uploaded"


def _split_pipe_query(query: str) -> list[str]:
    """Split a pipe-delimited query into individual terms."""
    if "|" in query:
        return [t.strip() for t in query.split("|") if t.strip()]
    return [query]


def resolve_extracted_path(rel_path: str) -> str:
    """Return absolute filesystem path for a markdown rel_path in EXTRACTED_DIR."""
    # Some callers pass the bare doc_id; resolve_doc returns the .md rel_path.
    # If rel_path is already absolute or points to an existing file, use it.
    if os.path.isabs(rel_path) and os.path.exists(rel_path):
        return rel_path
    direct = os.path.join(EXTRACTED_DIR, rel_path)
    if os.path.exists(direct):
        return direct
    # Fallback: try with .md extension if missing
    if not rel_path.endswith(".md"):
        direct_md = os.path.join(EXTRACTED_DIR, rel_path + ".md")
        if os.path.exists(direct_md):
            return direct_md
    return direct
