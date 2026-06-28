"""
Upload Namer — generates a display name and brief summary for an uploaded document.
Extracts a text snippet and calls an LLM. Uses a short timeout so uploads stay fast.
"""

import os
import re
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from agent.llm_reasoner import reason, get_llm_config

LLM_TIMEOUT_SECONDS = 45


def extract_text_snippet(file_path: str, max_chars: int = 1200) -> str:
    """Extract a text snippet from PDF or HTML for LLM naming."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            try:
                from PyPDF2 import PdfReader
            except ImportError:
                return ""

        try:
            reader = PdfReader(file_path)
            parts = []
            for page in reader.pages[:5]:
                text = page.extract_text() or ""
                parts.append(text)
                if len("".join(parts)) >= max_chars:
                    break
            return "".join(parts)[:max_chars]
        except Exception as e:
            print(f"[upload_namer] Failed to extract PDF text: {e}")
            return ""

    if ext == ".html":
        try:
            from utils.text_utils import strip_html_tags
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(max_chars * 2)
            return strip_html_tags(text)[:max_chars]
        except Exception as e:
            print(f"[upload_namer] Failed to read HTML: {e}")
            return ""

    return ""


def _call_llm_for_name(snippet: str) -> dict:
    """Internal blocking call to the LLM."""
    system = (
        "You are a document naming assistant. Given a text snippet from a financial or business document, "
        "produce a concise, human-readable filename (maximum 6 words, no dates) and a one-sentence summary. "
        "Respond ONLY with a JSON object in this exact format:\n"
        '{"filename": "Concise Descriptive Name", "summary": "One sentence describing the document."}'
    )
    prompt = f"Generate a filename and summary for the following document snippet:\n\n{snippet[:1200]}"

    # Use a smaller token budget for this quick naming task
    config = get_llm_config()
    config["max_tokens"] = 256

    result = reason(prompt=prompt, system=system, config=config, json_mode=True)
    if result.get("error"):
        raise RuntimeError(result["error"])

    parsed = result.get("parsed") or {}
    if not isinstance(parsed, dict):
        raise RuntimeError("Invalid JSON response")

    return parsed


def _fallback_name(original_filename: str) -> str:
    """Create a readable fallback from the original filename."""
    base = os.path.splitext(os.path.basename(original_filename))[0]
    # Replace underscores/hyphens with spaces and title-case
    return base.replace('_', ' ').replace('-', ' ').strip().title() or "Uploaded Document"


def generate_name_and_summary(snippet: str, original_filename: str = "") -> dict:
    """Call LLM to generate a filename and one-sentence summary, with a timeout."""
    if not snippet or not snippet.strip():
        return {
            "filename": _fallback_name(original_filename) if original_filename else "Uploaded Document",
            "summary": "No preview text available.",
            "fallback": True,
            "fallback_reason": "no_text",
        }

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call_llm_for_name, snippet)
            parsed = future.result(timeout=LLM_TIMEOUT_SECONDS)
    except FutureTimeoutError:
        print(f"[upload_namer] LLM naming timed out after {LLM_TIMEOUT_SECONDS}s; using fallback.")
        return {
            "filename": _fallback_name(original_filename) if original_filename else "Uploaded Document",
            "summary": "A summary is not available yet; you can still chat with the document.",
            "fallback": True,
            "fallback_reason": "timeout",
        }
    except Exception as e:
        print(f"[upload_namer] LLM naming failed: {e}; using fallback.")
        return {
            "filename": _fallback_name(original_filename) if original_filename else "Uploaded Document",
            "summary": "A summary could not be generated automatically; you can still chat with the document.",
            "fallback": True,
            "fallback_reason": "error",
        }

    filename = parsed.get("filename", "Uploaded Document").strip()
    summary = parsed.get("summary", "").strip()

    # Sanitize filename
    filename = re.sub(r'[\\/:*?"<>|]', "", filename)
    filename = re.sub(r'\s+', " ", filename).strip()
    if not filename:
        filename = _fallback_name(original_filename) if original_filename else "Uploaded Document"

    if not summary:
        summary = "No summary available."

    return {"filename": filename, "summary": summary, "fallback": False}


def build_metadata(original_filename: str, file_path: str) -> dict:
    """Generate full metadata object for an uploaded file."""
    snippet = extract_text_snippet(file_path)
    generated = generate_name_and_summary(snippet, original_filename=original_filename)
    date_str = datetime.now().strftime("%Y-%m-%d")
    display_name = f"{generated['filename']} {date_str}"

    metadata = {
        "original_filename": original_filename,
        "generated_filename": generated["filename"],
        "display_name": display_name,
        "summary": generated["summary"],
        "uploaded_at": datetime.now().isoformat(),
        "file_path": file_path,
    }
    if generated.get("fallback"):
        metadata["fallback"] = True
        metadata["fallback_reason"] = generated.get("fallback_reason", "unknown")
    return metadata


def load_metadata(stem: str, metadata_dir: str = "uploads/metadata") -> dict | None:
    """Load metadata JSON for a given file stem if it exists."""
    path = os.path.join(metadata_dir, f"{stem}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[upload_namer] Failed to load metadata: {e}")
    return None
