"""
RefMapper — assigns unique reference IDs (R1, R2, ...) to evidence chunks.
Resolves page numbers asynchronously (non-blocking) via page_resolver.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from agent.page_resolver import resolve_page
except ImportError:
    resolve_page = None


class RefMapper:
    """Assigns R1, R2, ... refs to evidence chunks. Resolves pages in background."""

    def __init__(self):
        self._refs = {}          # R1 -> {doc_id, page, snippet}
        self._counter = 0
        self._lock = threading.Lock()
        self._page_executor = ThreadPoolExecutor(max_workers=4)
        self._pending_futures = []  # Track background page resolution tasks

    def assign(self, results: list[dict]) -> dict:
        """
        Take ACT step results, assign R1, R2, ... immediately.
        Fire background page resolution (does NOT block).

        Args:
            results: list of {"tool": "...", "params": {...}, "result": ...}

        Returns:
            dict of {R1: snippet_context, ...} for LLM prompt injection
        """
        context = {}
        for r in results:
            result = r.get("result")
            source = r.get("tool", "")
            if result is None:
                continue

            # Extract text snippets
            snippets = self._extract_snippets(result, source)
            for snippet in snippets:
                with self._lock:
                    self._counter += 1
                    ref_id = f"R{self._counter}"
                    doc_id = snippet.get("doc_id", "")
                    text = snippet.get("text", "")[:300]

                    # Store ref immediately (no page yet)
                    self._refs[ref_id] = {
                        "doc_id": doc_id,
                        "page": None,
                        "text": text,
                        "source": source,
                    }

                    # Fire background page resolution
                    if resolve_page and doc_id:
                        future = self._page_executor.submit(
                            _resolve_page_safe, resolve_page, doc_id, text
                        )
                        self._pending_futures.append((ref_id, future))

                    # Build immediate context for LLM
                    context[ref_id] = {
                        "text": text,
                        "doc_id": doc_id,
                        "source": source,
                    }

        return context

    def _extract_snippets(self, result, source: str) -> list[dict]:
        """Extract text snippets from search tool results."""
        snippets = []
        if isinstance(result, list):
            for item in result[:10]:
                if isinstance(item, dict):
                    text = item.get("title") or item.get("text") or item.get("name", "")
                    doc_id = item.get("doc", "")
                    if not doc_id and "doc_path" in item:
                        doc_id = item["doc_path"].split("/")[-1].replace(".md", "")
                    if text:
                        snippets.append({"text": str(text)[:500], "doc_id": doc_id})
                elif isinstance(item, str):
                    snippets.append({"text": item[:500], "doc_id": ""})
        elif isinstance(result, dict):
            content = result.get("content", "")
            heading = result.get("heading", "")
            if content:
                base = heading or source
                # Split large content into individual snippet chunks
                lines = content.split("\n")
                for line in lines[:15]:
                    stripped = line.strip()
                    if len(stripped) > 10:
                        doc_id = heading or source
                        # Extract doc_id from heading path if possible
                        if hasattr(result, 'get'):
                            pass  # already handled
                        snippets.append({"text": stripped[:500], "doc_id": doc_id or ""})
            elif heading:
                snippets.append({"text": heading[:500], "doc_id": heading or ""})
        return snippets

    def collect_resolved(self) -> dict:
        """
        Wait for background page resolution and collect results.
        Returns updated refs dict with page numbers filled in.
        """
        for ref_id, future in self._pending_futures:
            try:
                page = future.result(timeout=5)
                if page and ref_id in self._refs:
                    self._refs[ref_id]["page"] = page
            except Exception:
                pass

        self._pending_futures = []
        return dict(self._refs)

    def get_refs(self) -> dict:
        """Get all refs (may or may not have page numbers yet)."""
        return dict(self._refs)

    def format_context(self, ref_context: dict) -> str:
        """Format ref context for LLM prompt injection."""
        if not ref_context:
            return ""
        lines = ["Evidence references:"]
        for ref_id, info in ref_context.items():
            doc = info.get("doc_id", "") or "doc"
            text = info.get("text", "")[:200]
            lines.append(f"  {ref_id}: {text} [{doc}]")
        return "\n".join(lines)

    def resolve_all_now(self) -> dict:
        """Block until all pending page resolutions complete. Returns refs with pages."""
        self.collect_resolved()
        return self._refs

    def shutdown(self):
        self._page_executor.shutdown(wait=False)


def _resolve_page_safe(resolve_fn, doc_id: str, text: str) -> int | None:
    """Wrapper for safe page resolution (catches all exceptions)."""
    try:
        return resolve_fn(doc_id, text)
    except Exception:
        return None