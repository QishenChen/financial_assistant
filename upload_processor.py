"""
Upload processor — runs MinerU extraction + indexing on uploads/raw/.
Called by server/api.py after files are uploaded.
Uses a file lock to prevent concurrent runs.
"""

import os
import shutil
import subprocess
import sys
import fcntl
import time
import threading
from pathlib import Path

UPLOAD_RAW_DIR = "uploads/raw"
UPLOAD_EXTRACTED_DIR = "uploads/extracted"
UPLOAD_INDEXED_DIR = "uploads/extracted/uploaded"
UPLOAD_STATE_FILE = "uploads/mineru_state.json"
LOCK_FILE = "uploads/.processor_lock"


def _acquire_lock():
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (OSError, IOError):
        lock_fd.close()
        return None


def _run(cmd, hard_timeout=900, idle_timeout=300):
    """Run a command with a hard timeout and an idle-output watchdog."""
    print(f"[upload_processor] Running: {' '.join(cmd)}")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    last_output = time.time()
    lock = threading.Lock()

    def _reader():
        nonlocal last_output
        try:
            for line in proc.stdout:
                line = line.rstrip()
                print(line)
                with lock:
                    last_output = time.time()
        except Exception as e:
            print(f"[upload_processor] Output reader error: {e}")

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    start = time.time()
    killed_reason = None
    while True:
        time.sleep(5)
        with lock:
            idle = time.time() - last_output
        if proc.poll() is not None:
            break
        if time.time() - start > hard_timeout:
            killed_reason = f"hard timeout ({hard_timeout}s)"
            break
        if idle > idle_timeout:
            killed_reason = f"no output for {idle_timeout}s"
            break

    if killed_reason:
        print(f"[upload_processor] Killing extraction: {killed_reason}")
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        reader_thread.join(timeout=5)
        return False

    reader_thread.join(timeout=5)
    if proc.returncode != 0:
        print(f"[upload_processor] Command failed with code {proc.returncode}: {' '.join(cmd)}")
        return False
    return True


def _sync_to_indexed_dir():
    """Copy extracted outputs into the indexed subdirectory so indexer/build_page_map find them."""
    os.makedirs(UPLOAD_INDEXED_DIR, exist_ok=True)
    copied = []
    if not os.path.isdir(UPLOAD_EXTRACTED_DIR):
        return copied

    for src in Path(UPLOAD_EXTRACTED_DIR).iterdir():
        if src.is_file() and (src.suffix == ".md" or src.name.endswith("_middle.json") or src.name.endswith("_model.json")):
            dst = Path(UPLOAD_INDEXED_DIR) / src.name
            shutil.copy2(str(src), str(dst))
            copied.append(src.name)
    if copied:
        print(f"[upload_processor] Synced {len(copied)} extracted file(s) to {UPLOAD_INDEXED_DIR}")
    return copied


def process_uploads():
    """Extract and index any unprocessed files in uploads/raw/."""
    os.makedirs(UPLOAD_RAW_DIR, exist_ok=True)
    os.makedirs(UPLOAD_EXTRACTED_DIR, exist_ok=True)
    os.makedirs(UPLOAD_INDEXED_DIR, exist_ok=True)

    print(f"[upload_processor] Starting upload processing...")
    lock_fd = _acquire_lock()
    if lock_fd is None:
        print("[upload_processor] Another extraction is already running; skipping.")
        return

    try:
        # Run MinerU extraction on uploads/raw/
        ok = _run([
            sys.executable, "-u", "run_mineru_extraction.py",
            "--source", UPLOAD_RAW_DIR,
            "--output", UPLOAD_EXTRACTED_DIR,
            "--state", UPLOAD_STATE_FILE,
        ])
        if not ok:
            return

        # Copy extracted outputs to the indexed subdirectory
        _sync_to_indexed_dir()

        # Rebuild indices so uploaded docs become queryable
        _run([sys.executable, "indexer.py"])
        _run([sys.executable, "build_page_map.py"])
        _run([sys.executable, "build_table_index.py"])

        # Make sure the running server reloads indices for Planner/tools
        try:
            from agent.tools._loader import invalidate_indices_cache
            invalidate_indices_cache()
            print("[upload_processor] Index cache invalidated; Planner will see new docs.")
        except Exception as e:
            print(f"[upload_processor] Could not invalidate index cache: {e}")

        print("[upload_processor] Upload processing complete.")
    finally:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    process_uploads()
