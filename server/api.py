"""
REST API — FastAPI server for the financial document intelligence platform.

All endpoints accept natural language queries. The Planner auto-detects task_type.

Endpoints:
    POST /upload    — upload PDFs, then extract + index in the background
    POST /query     — full Planner → Executor pipeline
    GET  /catalog   — structured document catalog (includes uploaded files)
    GET  /page-map  — page mapping for reference links
    GET  /upload    — upload page
    GET  /          — dialogue page

Run:
    pip install fastapi uvicorn python-multipart
    python3 server/api.py
"""

import sys
import os
import json
import shutil
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.executor import execute
from agent.llm_reasoner import get_llm_config
from agent.memory import (
    create_session,
    get_session,
    list_sessions,
    delete_session,
    clear_session_messages,
    append_message,
    get_messages,
    build_session_context,
    extract_and_store_memories,
)
from upload_namer import build_metadata, load_metadata, _fallback_name

try:
    from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
    from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.concurrency import run_in_threadpool
    from pydantic import BaseModel, Field
except ImportError:
    print("FastAPI not installed. Run: pip install fastapi uvicorn python-multipart")
    sys.exit(1)

# ── Paths ──
UPLOAD_RAW_DIR = "uploads/raw"
UPLOAD_EXTRACTED_DIR = "uploads/extracted"
UPLOAD_METADATA_DIR = "uploads/metadata"

# ── App setup ──
app = FastAPI(
    title="Financial Document Intelligence API",
    description="Natural language query interface for financial documents. Auto-detects task type.",
    version="1.0.0",
)

# CORS must be added before static mounts so Range requests from PDF.js work cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

# Serve frontend static files
app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")
# Serve raw PDFs for PDF.js viewer
app.mount("/raw", StaticFiles(directory="public_dataset_upload/raw"), name="raw")
# Serve uploaded PDFs
app.mount("/uploads", StaticFiles(directory=UPLOAD_RAW_DIR), name="uploads")
# Serve extracted markdown
app.mount("/extracted", StaticFiles(directory="public_dataset_upload/extracted"), name="extracted")


class QueryRequest(BaseModel):
    query: str = Field(..., description="Natural language question about financial documents")
    session_id: str | None = Field(None, description="Optional session ID; a new session is created if omitted")


class QueryResponse(BaseModel):
    session_id: str = ""
    task_type: str = ""
    overall_objective: str = ""
    output_shape: str = ""
    answer: str = ""
    confidence: float = 0.0
    steps_log: list = []
    file_path: str = ""
    token_usage: dict = {}
    error: str | None = None


def _run_and_respond(query: str, session_id: str | None = None) -> dict:
    """Execute a query in the context of a session and format the response."""
    if not session_id or not get_session(session_id):
        session_id = create_session()

    # Append user message before generating the answer.
    append_message(session_id, "user", query)

    # Build short-term + long-term memory context.
    session_context = build_session_context(session_id, query)

    result = execute(query, session_context=session_context)

    answer = result.get("answer", "")
    file_path = result.get("file_path", "")

    # Append assistant message.
    append_message(session_id, "assistant", answer, file_path=file_path or None)

    # Extract durable memories asynchronously in a background thread so the
    # response is not delayed.
    def _extract():
        try:
            extract_and_store_memories(query, answer)
        except Exception as e:
            print(f"[memory] extraction failed: {e}")

    import threading
    threading.Thread(target=_extract, daemon=True).start()

    return {
        "session_id": session_id,
        "task_type": result.get("task_type", ""),
        "overall_objective": result.get("overall_objective", ""),
        "output_shape": result.get("output_shape", "paragraph"),
        "answer": answer,
        "confidence": result.get("confidence", 0.0),
        "steps_log": result.get("steps_log", []),
        "file_path": file_path,
        "token_usage": result.get("token_usage", {}),
        "error": result.get("error"),
    }


def _sanitize_filename(name: str) -> str:
    """Keep only safe characters in uploaded filenames."""
    name = os.path.basename(name)
    return "".join(c for c in name if c.isalnum() or c in " ._-()").strip()


# Background metadata naming worker. Naming calls the LLM and can be slow/hang,
# so it is decoupled from the upload response.
_naming_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="upload_namer")

def _write_metadata(meta: dict, stem: str, metadata_dir: str):
    os.makedirs(metadata_dir, exist_ok=True)
    meta_path = os.path.join(metadata_dir, f"{stem}.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def _generate_metadata_async(safe_name: str, dest_path: str, metadata_dir: str, max_attempts: int = 2):
    """Generate LLM filename + summary in the background and update metadata."""
    def _run():
        stem = os.path.splitext(safe_name)[0]
        for attempt in range(1, max_attempts + 1):
            try:
                print(f"[upload] generating metadata for {safe_name} (attempt {attempt})")
                metadata = build_metadata(safe_name, dest_path)
                # If the LLM call timed out or errored, retry rather than keeping a fallback name.
                if metadata.get("fallback") and metadata.get("fallback_reason") in ("timeout", "error"):
                    raise RuntimeError(f"LLM naming returned fallback ({metadata.get('fallback_reason')})")
                _write_metadata(metadata, stem, metadata_dir)
                print(f"[upload] metadata saved for {safe_name}: {metadata.get('display_name')}")
                return
            except Exception as e:
                print(f"[upload] metadata attempt {attempt} failed for {safe_name}: {e}")
                if attempt < max_attempts:
                    time.sleep(5)
        # Preserve whatever fallback we ended up with so the UI still has a file entry.
        try:
            final_metadata = build_metadata(safe_name, dest_path)
            _write_metadata(final_metadata, stem, metadata_dir)
        except Exception as e:
            print(f"[upload] could not write final fallback metadata for {safe_name}: {e}")
        print(f"[upload] giving up on metadata generation for {safe_name}")
    _naming_executor.submit(_run)


def _build_structured_catalog():
    """Build a structured catalog of only user-uploaded files."""
    from agent.tools._loader import get_indices

    domains = {}

    # Indexed / extracted uploaded documents
    indices = get_indices()
    doc_registry = indices.get("doc_registry", {})
    by_id = doc_registry.get("by_id", {})
    if not by_id:
        by_id = doc_registry

    # Load page_map for accurate page counts (doc_registry does not store them)
    page_map_path = os.path.join("indices", "page_map.json")
    page_map_docs = {}
    if os.path.exists(page_map_path):
        try:
            with open(page_map_path, "r", encoding="utf-8") as f:
                page_map_docs = json.load(f).get("documents", {})
        except Exception as e:
            print(f"[catalog] failed to read page_map: {e}")

    def _page_count(rel_path: str) -> int:
        entry = page_map_docs.get(rel_path, {})
        return entry.get("total_pages", 0)

    # Load MinerU state so we can report queued/extracting/failed/ready status
    state_path = os.path.join(os.path.dirname(UPLOAD_RAW_DIR), "mineru_state.json")
    mineru_state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                mineru_state = json.load(f)
        except Exception as e:
            print(f"[catalog] failed to read mineru state: {e}")
    completed_set = set(mineru_state.get("completed", []))
    failed_set = set(mineru_state.get("failed", []))
    pending_batches = mineru_state.get("pending_batches", {})
    split_map = mineru_state.get("split_map", {})
    pending_files = set()
    for files in pending_batches.values():
        pending_files.update(files)

    def _raw_status(fname: str) -> str:
        """Return queued | extracting | failed | ready for a raw upload filename."""
        chunks = split_map.get(fname, [])
        if fname in failed_set or any(c in failed_set for c in chunks):
            return "failed"
        if fname in completed_set:
            return "ready"
        if fname in pending_files or any(c in pending_files for c in chunks):
            return "extracting"
        return "queued"

    for doc_path, info in by_id.items():
        if not isinstance(info, dict):
            continue
        domain = info.get("domain", "unknown")
        # Only include documents that come from the upload area
        if domain != "uploaded":
            continue
        domains.setdefault(domain, []).append({
            "name": info.get("friendly_name", doc_path),
            "id": info.get("doc_id", ""),
            "pages": _page_count(info.get("rel_path", "")),
            "source": "indexed",
            "status": "ready",
        })

    # Augment uploaded entries with LLM-generated display names from metadata
    if "uploaded" in domains:
        for doc in domains["uploaded"]:
            meta = load_metadata(doc["id"], UPLOAD_METADATA_DIR)
            if meta:
                doc["name"] = meta.get("display_name") or meta.get("generated_filename") or doc["name"]
                doc["summary"] = meta.get("summary", "")
                doc["metadata"] = meta

    # Uploaded raw files that are not yet reflected in the doc registry
    indexed_ids = {d["id"] for d in domains.get("uploaded", [])}

    uploaded_raw = []
    if os.path.isdir(UPLOAD_RAW_DIR):
        for fname in sorted(os.listdir(UPLOAD_RAW_DIR)):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in (".pdf", ".html"):
                continue
            base = os.path.splitext(fname)[0]
            if base in indexed_ids:
                continue
            meta = load_metadata(base, UPLOAD_METADATA_DIR)
            entry = {
                "name": fname,
                "id": base,
                "pages": 0,
                "source": "uploaded",
                "status": _raw_status(fname),
            }
            if meta:
                entry["name"] = meta.get("display_name") or meta.get("generated_filename") or fname
                entry["summary"] = meta.get("summary", "")
                entry["metadata"] = meta
            uploaded_raw.append(entry)

    if uploaded_raw:
        domains.setdefault("uploaded", []).extend(uploaded_raw)

    # Build legacy text catalog for backward compatibility (only uploads)
    text_lines = ["Available documents by domain:"]
    for domain, docs in sorted(domains.items()):
        text_lines.append(f"\n[{domain}] — {len(docs)} document(s):")
        for d in docs:
            text_lines.append(f"  - {d['name']} (id: {d['id']}, ~{d.get('pages', 0)} pages)")
    text_catalog = "\n".join(text_lines)

    return {"catalog": text_catalog, "domains": domains}


# ── Background upload processing queue ──
_upload_event = threading.Event()


def _pending_raw_files():
    """Return raw PDF/HTML filenames that have not been extracted/indexed yet."""
    pending = []
    if not os.path.isdir(UPLOAD_RAW_DIR):
        return pending

    # Load MinerU state to distinguish completed/failed files
    completed = set()
    failed = set()
    state_path = os.path.join(os.path.dirname(UPLOAD_RAW_DIR), "mineru_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            completed = set(state.get("completed", []))
            failed = set(state.get("failed", []))
        except Exception as e:
            print(f"[upload] failed to read mineru state: {e}")

    indexed_dir = os.path.join(UPLOAD_EXTRACTED_DIR, "uploaded")
    for fname in sorted(os.listdir(UPLOAD_RAW_DIR)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in (".pdf", ".html"):
            continue
        if fname in completed or fname in failed:
            continue
        stem = os.path.splitext(fname)[0]
        if os.path.exists(os.path.join(indexed_dir, f"{stem}.md")):
            continue
        pending.append(fname)
    return pending


def _upload_worker():
    """Daemon worker that keeps processing uploads until the queue is empty."""
    while True:
        _upload_event.wait()
        while _upload_event.is_set():
            try:
                from upload_processor import process_uploads
                print("[upload] background processing started")
                process_uploads()
                print("[upload] background processing finished")
            except Exception as e:
                print(f"[upload] background processing failed: {e}")
                time.sleep(5)
                continue

            remaining = _pending_raw_files()
            if not remaining:
                _upload_event.clear()
                print("[upload] no pending uploads, worker going idle")
            else:
                print(f"[upload] {len(remaining)} file(s) still pending, retrying...")
                time.sleep(3)


def _start_upload_sweep():
    """Periodically rescan for uploads that missed a trigger (e.g. after restart)."""
    def _sweep():
        while True:
            time.sleep(60)
            pending = _pending_raw_files()
            if pending:
                print(f"[upload] sweep found {len(pending)} pending file(s), queueing processing")
                _upload_event.set()
    threading.Thread(target=_sweep, daemon=True).start()


def _trigger_upload_processing():
    """Signal the background worker to run extraction + indexing."""
    _upload_event.set()


# Start the worker at import time so it can service upload events.
threading.Thread(target=_upload_worker, daemon=True).start()


@app.post("/upload")
async def upload_files(files: list[UploadFile] = File(...)):
    """Upload PDFs to uploads/raw/ and trigger background extraction + indexing."""
    os.makedirs(UPLOAD_RAW_DIR, exist_ok=True)
    os.makedirs(UPLOAD_EXTRACTED_DIR, exist_ok=True)
    os.makedirs(UPLOAD_METADATA_DIR, exist_ok=True)

    saved = []
    metadata_list = []
    for file in files:
        if not file.filename:
            continue
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in (".pdf", ".html"):
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")
        safe_name = _sanitize_filename(file.filename)
        if not safe_name:
            safe_name = "uploaded.pdf"
        dest_path = os.path.join(UPLOAD_RAW_DIR, safe_name)
        # Avoid overwriting by appending a counter
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(safe_name)
            counter = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(UPLOAD_RAW_DIR, f"{base}_{counter}{ext}")
                counter += 1

        try:
            with open(dest_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            saved_name = os.path.basename(dest_path)
            saved.append(saved_name)
            stem = os.path.splitext(saved_name)[0]

            # Write an initial placeholder immediately so the upload response and
            # UI are fast. The LLM-generated name/summary will replace it in the
            # background, and the sidebar will auto-refresh to show the new name.
            date_str = datetime.now().strftime("%Y-%m-%d")
            placeholder_metadata = {
                "original_filename": saved_name,
                "generated_filename": _fallback_name(saved_name),
                "display_name": f"{_fallback_name(saved_name)} {date_str}",
                "summary": "A name and summary are being generated; this will update shortly.",
                "uploaded_at": datetime.now().isoformat(),
                "file_path": dest_path,
            }
            _write_metadata(placeholder_metadata, stem, UPLOAD_METADATA_DIR)
            metadata_list.append(placeholder_metadata)

            # Generate LLM filename + summary asynchronously so slow LLM calls
            # do not block the upload response or extraction queue.
            _generate_metadata_async(saved_name, dest_path, UPLOAD_METADATA_DIR)
        finally:
            await file.close()

    if not saved:
        raise HTTPException(status_code=400, detail="No valid files uploaded")

    _trigger_upload_processing()

    return {
        "ok": True,
        "files": saved,
        "metadata": metadata_list,
        "redirect": "/",
        "message": "Files uploaded. Extraction and indexing are running in the background.",
    }


@app.post("/query", response_model=QueryResponse)
async def query_endpoint(req: QueryRequest):
    """Full Planner → Executor pipeline. Planner auto-detects task type."""
    # Executor makes synchronous LLM/tool calls; run it in a thread so the
    # event loop stays responsive to other requests.
    return await run_in_threadpool(_run_and_respond, req.query, req.session_id)


@app.post("/sessions/new")
async def new_session():
    """Create a new chat session."""
    session_id = create_session()
    return {"session_id": session_id, "title": "New chat", "created_at": time.time()}


@app.get("/sessions")
async def sessions_list():
    """List recent chat sessions."""
    sessions = list_sessions()
    return {"sessions": sessions}


@app.get("/sessions/{session_id}")
async def session_detail(session_id: str):
    """Get session metadata and full message history."""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = get_messages(session_id)
    return {"session": dict(session), "messages": messages}


@app.post("/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    """Clear all messages in a session but keep the session itself."""
    if not get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    clear_session_messages(session_id)
    return {"ok": True}


@app.delete("/sessions/{session_id}")
async def remove_session(session_id: str):
    """Delete a session and all its messages."""
    if not get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    delete_session(session_id)
    return {"ok": True}


@app.get("/health")
async def health():
    """Health check."""
    config = get_llm_config()
    return {
        "status": "ok",
        "llm_model": config.get("model", "unknown"),
        "llm_configured": bool(config.get("api_key")),
    }


@app.get("/catalog")
async def catalog():
    """List available document domains, including uploaded files."""
    return _build_structured_catalog()


@app.get("/page-map")
async def page_map():
    """Get page mapping for document references."""
    page_map_path = os.path.join("indices", "page_map.json")
    if os.path.exists(page_map_path):
        with open(page_map_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"documents": {}}


@app.get("/upload")
async def serve_upload_page():
    """Serve the file upload page."""
    with open("frontend/upload.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/")
async def serve_frontend():
    """Serve the dialogue / chat app."""
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    _start_upload_sweep()
    uvicorn.run(app, host="0.0.0.0", port=8000)
