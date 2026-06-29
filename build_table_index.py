#!/usr/bin/env python3
"""
Build table index for the extracted markdown corpus.

Scans uploads/extracted/uploaded/*.md, extracts Markdown and HTML tables,
maps each table to its enclosing heading, and writes indices/table_index.json
with the same schema the search_tables tool expects.
"""

import json
import os
import re
import uuid
from pathlib import Path

from agent.tools._shared import EXTRACTED_DIR
from utils.text_utils import parse_html_table_cells, extract_unit_hint, strip_html_tags

INDICES_DIR = "indices"
TABLE_INDEX_PATH = os.path.join(INDICES_DIR, "table_index.json")
HEADING_INDEX_PATH = os.path.join(INDICES_DIR, "heading_index.json")


def _load_heading_index() -> dict:
    if not os.path.exists(HEADING_INDEX_PATH):
        return {"documents": {}}
    with open(HEADING_INDEX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _is_markdown_table_line(line: str) -> bool:
    return "|" in line.strip()


def _is_markdown_separator_row(cells: list[str]) -> bool:
    """A markdown separator row looks like :---|---:|:---:|."""
    if not cells:
        return False
    return all(re.fullmatch(r"[:\- ]+", c.strip()) for c in cells if c.strip())


def _parse_markdown_table(lines: list[str]) -> list[list[str]] | None:
    """Parse a consecutive block of markdown-table lines into rows of cells."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            line = "|" + line
        if not line.endswith("|"):
            line = line + "|"
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if _is_markdown_separator_row(cells):
            continue
        # Skip rows that became empty after stripping separator-like content
        if any(c.strip() for c in cells):
            rows.append(cells)
    return rows if rows else None


def _extract_html_tables(text: str, start_line: int = 0) -> list[tuple[int, str]]:
    """Return (line_offset, html_block) for each <table>...</table> block."""
    results = []
    # Split text into lines to estimate line numbers
    lines = text.split("\n")
    # Use regex to find table blocks across the whole text, then map back to line.
    for m in re.finditer(r"<table[^>]*>.*?</table>", text, re.DOTALL | re.IGNORECASE):
        block_start = m.start()
        line_idx = text[:block_start].count("\n")
        results.append((start_line + line_idx, m.group(0)))
    return results


def _extract_markdown_table_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Find consecutive markdown-table line blocks. Returns (start_line, block_lines)."""
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        if _is_markdown_table_line(lines[i]):
            start = i
            while i < n and _is_markdown_table_line(lines[i]):
                i += 1
            block_lines = lines[start:i]
            blocks.append((start, block_lines))
        else:
            i += 1
    return blocks


def _find_heading_for_line(line_num: int, headings: list[dict]) -> dict | None:
    """Return the deepest heading whose span contains line_num."""
    best = None
    for h in headings:
        ls = h.get("line_start", 0)
        le = h.get("line_end", 0)
        if ls <= line_num < le:
            if best is None or ls > best.get("line_start", 0):
                best = h
    return best


def _build_table_record(
    doc_path: str,
    rows: list[list[str]],
    line_num: int,
    heading: dict | None,
    context_before: str,
) -> dict | None:
    if not rows or len(rows) < 1:
        return None

    headers = rows[0]
    data = rows[1:]

    # Skip single-row tables
    if not data:
        return None

    heading_title = heading.get("title", "") if heading else ""
    heading_path = heading.get("path", [heading_title]) if heading else [heading_title]

    # Derive a human-readable name from context or heading
    name = heading_title or "表格"
    first_cell = headers[0] if headers else ""
    if first_cell and len(first_cell) < 60:
        name = f"{heading_title or ''} — {first_cell}".strip(" —")

    return {
        "table_id": f"T_{uuid.uuid4().hex[:8].upper()}",
        "doc_path": doc_path,
        "name": name,
        "heading_path": heading_path,
        "heading_title": heading_title,
        "line_num": line_num,
        "row_count": len(data),
        "col_count": len(headers),
        "headers": headers,
        "data": data,
        "unit": extract_unit_hint(context_before) or "",
        "context_before": context_before[:500],
    }


def _collect_context_before(lines: list[str], start_line: int, max_chars: int = 300) -> str:
    """Collect a few non-empty lines immediately before a table."""
    parts = []
    chars = 0
    for i in range(start_line - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        # Skip markdown separator-ish lines
        if re.fullmatch(r"[\|\-\:\s]+", stripped):
            continue
        parts.insert(0, stripped)
        chars += len(stripped)
        if chars >= max_chars or len(parts) >= 5:
            break
    return "\n".join(parts)


def index_document(doc_path: str, abs_path: str, headings: list[dict]) -> list[dict]:
    """Extract all tables from a single markdown file."""
    tables = []
    with open(abs_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    text = "".join(lines)

    # Markdown tables
    for start_line, block_lines in _extract_markdown_table_blocks(lines):
        rows = _parse_markdown_table(block_lines)
        if not rows:
            continue
        # Skip tables that are likely inside HTML blocks already captured below
        heading = _find_heading_for_line(start_line, headings)
        context = _collect_context_before(lines, start_line)
        t = _build_table_record(doc_path, rows, start_line, heading, context)
        if t:
            tables.append(t)

    # HTML tables
    for line_num, html_block in _extract_html_tables(text):
        html_rows = parse_html_table_cells(html_block)
        if not html_rows:
            continue
        # Avoid duplicating a table we already captured as markdown (same line)
        if any(abs(t["line_num"] - line_num) < 3 for t in tables):
            continue
        heading = _find_heading_for_line(line_num, headings)
        context = _collect_context_before(lines, line_num)
        t = _build_table_record(doc_path, html_rows, line_num, heading, context)
        if t:
            tables.append(t)

    return tables


def build_table_index() -> dict:
    """Build the full table index for the configured EXTRACTED_DIR."""
    heading_index = _load_heading_index()
    heading_docs = heading_index.get("documents", {})

    all_tables = []
    by_doc: dict[str, list[str]] = {}
    by_heading: dict[str, list[str]] = {}

    for root, _, files in os.walk(EXTRACTED_DIR):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            abs_path = os.path.join(root, fname)
            doc_path = os.path.relpath(abs_path, EXTRACTED_DIR)
            print(f"  Indexing tables: {doc_path}")

            headings = heading_docs.get(doc_path, [])
            doc_tables = index_document(doc_path, abs_path, headings)

            for t in doc_tables:
                all_tables.append(t)
                by_doc.setdefault(doc_path, []).append(t["table_id"])
                key = f"{doc_path}::{t['heading_title']}"
                by_heading.setdefault(key, []).append(t["table_id"])

    return {
        "tables": all_tables,
        "by_doc": by_doc,
        "by_heading": by_heading,
        "total": len(all_tables),
    }


def main():
    print("Building table index from extracted markdown files...")
    os.makedirs(INDICES_DIR, exist_ok=True)

    index = build_table_index()

    with open(TABLE_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"Saved {TABLE_INDEX_PATH}: {index['total']} tables from {len(index['by_doc'])} documents")


if __name__ == "__main__":
    main()
