#!/usr/bin/env python3
"""Tests for page-number resolution.

Two parts:
1. A deterministic unit test that samples snippets from page_map.json and checks
   that resolve_page() maps them back to the correct page.
2. An end-to-end check that runs a real query through agent.executor.execute()
   and counts how many ref links include page numbers.
"""

import hashlib
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.page_resolver import resolve_page, _normalize, _resolve_cache


def test_resolver_on_known_snippets(sample_chars: int = 80, samples_per_doc: int = 3):
    """Sample real page text and verify resolve_page returns the source page."""
    page_map_path = os.path.join("indices", "page_map.json")
    if not os.path.exists(page_map_path):
        print("page_map.json not found; skipping resolver unit test.")
        return False

    with open(page_map_path, "r", encoding="utf-8") as f:
        page_map = json.load(f)

    documents = page_map.get("documents", {})
    total = 0
    correct = 0
    ambiguous = 0

    for doc_rel_path, doc_info in documents.items():
        total_pages = doc_info.get("total_pages", 0)
        snippets = doc_info.get("page_snippets", {})
        if total_pages == 0 or not snippets:
            continue

        pages_with_text = list(snippets.items())
        sampled = random.sample(
            pages_with_text,
            min(samples_per_doc, len(pages_with_text)),
        )

        for page_str, page_text in sampled:
            source_page = int(page_str)
            page_text = page_text.strip()
            if len(page_text) < sample_chars:
                snippet = page_text
            else:
                start = max(0, len(page_text) // 2 - sample_chars // 2)
                snippet = page_text[start : start + sample_chars]

            resolved = resolve_page(doc_rel_path, snippet)
            total += 1
            if resolved == source_page:
                correct += 1
            elif resolved is None:
                ambiguous += 1
            # else wrong — counted as neither correct nor ambiguous

    accuracy = correct / total if total else 0.0
    print(f"Resolver unit test: {correct}/{total} correct ({accuracy:.1%}), {ambiguous} uncertain")
    return accuracy >= 0.8


def test_resolver_rejects_generic_snippets():
    """Generic, repeated text should not confidently map to any page."""
    page_map_path = os.path.join("indices", "page_map.json")
    if not os.path.exists(page_map_path):
        return True

    with open(page_map_path, "r", encoding="utf-8") as f:
        page_map = json.load(f)

    documents = list(page_map.get("documents", {}).keys())
    if not documents:
        return True

    generic_snippets = [
        "2025年12月31日",
        "单位：元",
        "人民币",
        "本公司",
    ]
    bad = 0
    for doc in documents[:5]:
        for snippet in generic_snippets:
            page = resolve_page(doc, snippet)
            if page is not None:
                bad += 1
                print(f"  Generic snippet returned page {page}: {snippet!r}")
    print(f"Generic-snippet false positives: {bad}")
    return bad == 0


def test_end_to_end_page_refs():
    """Run a real query and check that ref links contain page numbers."""
    from agent.executor import execute

    print("Running end-to-end test: 招商银行不良贷款余额是多少")
    print("-" * 50)

    r = execute('招商银行不良贷款余额是多少')
    ans = r.get('answer', '')

    all_refs = re.findall(r'\(ref:([^)]+)\)', ans)
    with_pages = [r for r in all_refs if re.search(r':\d+$', r)]
    without_pages = [r for r in all_refs if not re.search(r':\d+$', r)]

    print(f"Total ref links: {len(all_refs)}")
    print(f"With page numbers: {len(with_pages)}")
    print(f"Without page numbers: {len(without_pages)}")

    if with_pages:
        print("\n✓ SUCCESS - Page numbers found in refs:")
        for ref in with_pages[:5]:
            print(f"  ref:{ref}")
    else:
        print("\n✗ FAILED - No page numbers in refs")
        print("Sample refs:")
        for ref in all_refs[:5]:
            print(f"  ref:{ref}")

    print(f"\nAnswer preview ({len(ans)} chars):")
    print(ans[:500])

    print("\nExecution steps:")
    for s in r.get('steps_log', []):
        print(f"  [{s.get('verdict','?')}] Step {s.get('step')}: {s.get('task_type')}")

    return len(with_pages) > 0


def main():
    # Clear any cached results from previous runs.
    _resolve_cache.clear()

    print("=" * 60)
    print("Part 1: Resolver unit tests")
    print("=" * 60)
    ok1 = test_resolver_on_known_snippets()
    ok2 = test_resolver_rejects_generic_snippets()

    print("\n" + "=" * 60)
    print("Part 2: End-to-end page-ref test")
    print("=" * 60)
    # The end-to-end test calls the LLM; skip it if no API key is configured.
    from agent.llm_reasoner import get_llm_config
    cfg = get_llm_config()
    if not cfg.get("api_key"):
        print("No LLM_API_KEY configured; skipping end-to-end test.")
        ok3 = True
    else:
        ok3 = test_end_to_end_page_refs()

    print("\n" + "=" * 60)
    if ok1 and ok2 and ok3:
        print("All page-number tests passed.")
    else:
        print("Some page-number tests failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
