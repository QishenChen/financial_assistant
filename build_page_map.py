#!/usr/bin/env python3
"""
Build page mapping for markdown documents using MinerU layout.json/middle.json.
Maps text snippets to PDF pages.
Output: indices/page_map.json
"""

import json
import os
import re
from pathlib import Path

EXTRACTED_DIR = "uploads/extracted/uploaded"
INDICES_DIR = "indices"
PAGE_MAP_PATH = os.path.join(INDICES_DIR, "page_map.json")


def find_middle_json(rel_path: str) -> str | None:
    """Find the middle.json/layout.json file for a given markdown document."""
    base = rel_path.rsplit('.', 1)[0]
    candidates = [
        base + '_middle.json',
        base + '_layout.json',
    ]
    for c in candidates:
        path = os.path.join(EXTRACTED_DIR, c)
        if os.path.exists(path):
            return path
    return None


def load_pages(middle_path: str) -> list[dict]:
    """Load middle.json and extract text per page."""
    with open(middle_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    pages = []
    pdf_info = data.get("pdf_info", [])
    for page_data in pdf_info:
        page_idx = page_data.get("page_idx", 0)
        page_size = page_data.get("page_size", [])

        # Extract text from para_blocks → lines → spans → content
        all_text = []
        for block in page_data.get("para_blocks", []):
            for line in block.get("lines", []):
                line_text = []
                for span in line.get("spans", []):
                    content = span.get("content", "") or span.get("text", "")
                    if content:
                        line_text.append(content)
                if line_text:
                    all_text.append("".join(line_text))

        pages.append({
            "page": page_idx + 1,  # 1-based
            "page_size": page_size,
            "text": " ".join(all_text),
        })
    return pages


def _load_doc_domains() -> dict:
    """Load a mapping markdown rel_path -> domain from the doc registry."""
    registry_path = os.path.join(INDICES_DIR, "doc_registry.json")
    domains = {}
    if not os.path.exists(registry_path):
        return domains
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            registry = json.load(f)
        for info in registry.get("by_id", {}).values():
            if isinstance(info, dict):
                domains[info.get("rel_path", "")] = info.get("domain", "unknown")
    except Exception as e:
        print(f"    ⚠ Could not load doc_registry for domains: {e}")
    return domains


def build_page_map() -> dict:
    """Build page_map.json for all extracted documents with middle.json."""
    page_map = {"version": 2, "documents": {}}
    doc_domains = _load_doc_domains()

    for root, dirs, files in os.walk(EXTRACTED_DIR):
        for fname in files:
            if not fname.endswith('.md'):
                continue
            rel_path = os.path.relpath(os.path.join(root, fname), EXTRACTED_DIR)

            middle_path = find_middle_json(rel_path)
            if not middle_path:
                continue

            print(f"  Mapping: {rel_path}")
            try:
                pages = load_pages(middle_path)
            except Exception as e:
                print(f"    ⚠ Error: {e}")
                continue

            # Store full page text for more accurate page matching.
            # Note: this increases index size; rebuild if disk/memory becomes a concern.
            page_snippets = {}
            for p in pages:
                text = p["text"]
                if text:
                    page_snippets[str(p["page"])] = text

            # Compute raw PDF path
            raw_rel = rel_path.replace('.md', '.pdf').replace('extracted/', 'raw/')
            domain = doc_domains.get(rel_path, "unknown")

            page_map["documents"][rel_path] = {
                "rel_path": rel_path,
                "total_pages": len(pages),
                "page_snippets": page_snippets,
                "raw_rel_path": raw_rel,
                "domain": domain,
            }

    # Also mark documents without middle.json
    for root, dirs, files in os.walk(EXTRACTED_DIR):
        for fname in files:
            if not fname.endswith('.md'):
                continue
            rel_path = os.path.relpath(os.path.join(root, fname), EXTRACTED_DIR)
            if rel_path not in page_map["documents"]:
                raw_rel = rel_path.replace('.md', '.pdf').replace('extracted/', 'raw/')
                domain = doc_domains.get(rel_path, "unknown")
                page_map["documents"][rel_path] = {
                    "rel_path": rel_path,
                    "total_pages": 0,
                    "page_snippets": {},
                    "raw_rel_path": raw_rel,
                    "domain": domain,
                    "_note": "No middle.json found — page references unavailable",
                }

    return page_map


def main():
    print("Building page map from layout.json/middle.json files...")
    page_map = build_page_map()

    os.makedirs(INDICES_DIR, exist_ok=True)
    with open(PAGE_MAP_PATH, 'w', encoding='utf-8') as f:
        json.dump(page_map, f, ensure_ascii=False, indent=2)

    mapped = sum(1 for d in page_map["documents"].values() if d.get("total_pages", 0) > 0)
    print(f"Saved: {mapped} documents with page mapping, {len(page_map['documents'])} total")


if __name__ == "__main__":
    main()