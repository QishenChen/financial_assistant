"""
Memory and session management for the financial assistant.

Uses SQLite for persistence and (optionally) the configured LLM provider's
embedding endpoint for semantic memory retrieval. Falls back to keyword overlap
if embeddings are unavailable.
"""

import json
import math
import os
import sqlite3
import time
import uuid
from typing import List, Dict, Any

from agent.llm_reasoner import reason, get_llm_config
from utils.text_utils import tokenize_chinese, fuzzy_match

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "memory.db")
DEFAULT_EMBEDDING_MODEL = "text-embedding-v3"
MAX_MEMORIES = 200


def _ensure_db() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'default',
            title TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            file_path TEXT,
            created_at REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'default',
            content TEXT NOT NULL UNIQUE,
            category TEXT,
            importance INTEGER NOT NULL DEFAULT 3,
            embedding TEXT,
            created_at REAL NOT NULL,
            last_accessed_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, last_accessed_at DESC);
        """
    )
    conn.commit()


# ── Sessions ──

def create_session(user_id: str = "default", title: str = "") -> str:
    conn = _ensure_db()
    now = time.time()
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, user_id, title or "New chat", now, now),
    )
    conn.commit()
    conn.close()
    return session_id


def get_session(session_id: str) -> Dict[str, Any] | None:
    conn = _ensure_db()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def list_sessions(user_id: str = "default", limit: int = 50) -> List[Dict[str, Any]]:
    conn = _ensure_db()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_session_title(session_id: str, title: str):
    conn = _ensure_db()
    conn.execute(
        "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
        (title, time.time(), session_id),
    )
    conn.commit()
    conn.close()


def delete_session(session_id: str):
    conn = _ensure_db()
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


def clear_session_messages(session_id: str):
    conn = _ensure_db()
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()


# ── Messages ──

def append_message(
    session_id: str,
    role: str,
    content: str,
    file_path: str | None = None,
    auto_title: bool = True,
) -> int:
    conn = _ensure_db()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, file_path, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content[:20000], file_path, now),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (now, session_id),
    )
    # Auto-title a new session from the first user message.
    if auto_title and role == "user":
        existing = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user'",
            (session_id,),
        ).fetchone()[0]
        if existing <= 1:
            title = (content[:40] + "...") if len(content) > 40 else content
            conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ? AND (title IS NULL OR title = 'New chat')",
                (title, session_id),
            )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()
    return msg_id


def get_messages(session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    conn = _ensure_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_messages(session_id: str, limit: int = 6) -> List[Dict[str, Any]]:
    conn = _ensure_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


# ── Embeddings ──

def _get_embedding_endpoint(config: Dict[str, Any]) -> str:
    base = config.get("api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    return f"{base}/embeddings"


def get_embeddings(texts: List[str]) -> List[List[float]]:
    """
    Call the configured LLM provider's embedding endpoint.
    Raises on failure so callers can fall back to keyword retrieval.
    """
    if not texts:
        return []
    config = get_llm_config()
    api_key = config.get("api_key", "")
    if not api_key:
        raise RuntimeError("No API key configured for embeddings")

    import requests

    model = os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    url = _get_embedding_endpoint(config)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "input": [t[:2000] for t in texts]}

    resp = requests.post(url, headers=headers, json=payload, timeout=(10, 60))
    resp.raise_for_status()
    data = resp.json()
    embeddings = data.get("data", [])
    # Sort by index because some APIs do not preserve order.
    embeddings = sorted(embeddings, key=lambda x: x.get("index", 0))
    return [item["embedding"] for item in embeddings]


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _keyword_overlap_score(query: str, text: str) -> float:
    q_tokens = set(tokenize_chinese(query))
    t_tokens = set(tokenize_chinese(text))
    if not q_tokens:
        return 0.0
    overlap = len(q_tokens & t_tokens)
    return overlap / len(q_tokens)


# ── Memories ──

def add_memory(
    content: str,
    category: str = "fact",
    importance: int = 3,
    user_id: str = "default",
    embedding: List[float] | None = None,
) -> int:
    content = content.strip()
    if not content:
        return -1

    conn = _ensure_db()
    now = time.time()

    # Deduplicate by exact content.
    existing = conn.execute(
        "SELECT id FROM memories WHERE user_id = ? AND content = ?",
        (user_id, content),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE memories SET last_accessed_at = ?, importance = MAX(importance, ?) WHERE id = ?",
            (now, importance, existing["id"]),
        )
        conn.commit()
        mem_id = existing["id"]
        conn.close()
        return mem_id

    emb_json = None
    if embedding is None:
        try:
            emb = get_embeddings([content])
            if emb:
                emb_json = json.dumps(emb[0], ensure_ascii=False)
        except Exception as e:
            print(f"[memory] embedding failed for memory: {e}")
    else:
        emb_json = json.dumps(embedding, ensure_ascii=False)

    cur = conn.execute(
        "INSERT INTO memories (user_id, content, category, importance, embedding, created_at, last_accessed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, content, category, importance, emb_json, now, now),
    )
    conn.commit()
    mem_id = cur.lastrowid
    conn.close()

    _cleanup_memories(user_id)
    return mem_id


def search_memories(
    query: str,
    user_id: str = "default",
    k: int = 5,
) -> List[Dict[str, Any]]:
    conn = _ensure_db()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? ORDER BY last_accessed_at DESC LIMIT ?",
        (user_id, MAX_MEMORIES * 2),
    ).fetchall()
    conn.close()

    if not rows:
        return []

    scored = []
    try:
        query_emb = get_embeddings([query])[0]
        for r in rows:
            emb_json = r["embedding"]
            if emb_json:
                mem_emb = json.loads(emb_json)
                score = _cosine_similarity(query_emb, mem_emb)
            else:
                score = _keyword_overlap_score(query, r["content"])
            scored.append((score, dict(r)))
    except Exception as e:
        print(f"[memory] embedding retrieval failed, using keyword fallback: {e}")
        for r in rows:
            score = _keyword_overlap_score(query, r["content"])
            scored.append((score, dict(r)))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [item[1] for item in scored[:k]]

    # Update last_accessed for retrieved memories.
    if top:
        conn = _ensure_db()
        ids = [str(m["id"]) for m in top]
        conn.execute(
            f"UPDATE memories SET last_accessed_at = ? WHERE id IN ({','.join(ids)})",
            (time.time(),),
        )
        conn.commit()
        conn.close()
    return top


def _cleanup_memories(user_id: str):
    conn = _ensure_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ?",
        (user_id,),
    ).fetchone()[0]
    if count > MAX_MEMORIES:
        to_delete = count - MAX_MEMORIES
        conn.execute(
            """
            DELETE FROM memories WHERE id IN (
                SELECT id FROM memories WHERE user_id = ?
                ORDER BY importance ASC, last_accessed_at ASC, created_at ASC
                LIMIT ?
            )
            """,
            (user_id, to_delete),
        )
        conn.commit()
    conn.close()


def list_memories(user_id: str = "default", limit: int = 100) -> List[Dict[str, Any]]:
    conn = _ensure_db()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? ORDER BY last_accessed_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Memory extraction ──

def extract_and_store_memories(
    query: str,
    answer: str,
    user_id: str = "default",
    config: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Use a cheap LLM call to extract durable facts from the last turn."""
    if config is None:
        config = get_llm_config()

    system = (
        "You are a memory extraction assistant. Given the user's question and the assistant's answer, "
        "extract concise, durable facts that should be remembered for future conversations. "
        "Categories: preference (user likes/formats), entity (companies, people, metrics), "
        "fact (objective data), constraint (rules the user set). "
        "Return ONLY JSON: {\"memories\": [{\"content\":\"...\",\"category\":\"...\",\"importance\":1-5}]}"
    )
    prompt = f"User: {query[:1000]}\n\nAssistant: {answer[:2000]}\n\nExtract memories."

    result = reason(prompt=prompt, system=system, config=config, json_mode=True)
    parsed = result.get("parsed") or {}
    memories = parsed.get("memories", []) if isinstance(parsed, dict) else []

    stored = []
    for m in memories:
        if not isinstance(m, dict):
            continue
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        category = str(m.get("category", "fact")).lower()
        importance = max(1, min(5, int(m.get("importance", 3))))
        add_memory(content, category=category, importance=importance, user_id=user_id)
        stored.append({"content": content, "category": category, "importance": importance})
    return stored


# ── Context assembly ──

def build_session_context(
    session_id: str,
    current_query: str,
    user_id: str = "default",
    max_messages: int = 6,
    max_memories: int = 5,
) -> str:
    """Build a concise context block for Planner / Executor prompts."""
    parts = []

    recent = get_recent_messages(session_id, limit=max_messages)
    if recent:
        parts.append("Recent conversation history:")
        for m in recent:
            prefix = "User" if m["role"] == "user" else "Assistant"
            snippet = m["content"][:400].replace("\n", " ")
            parts.append(f"  {prefix}: {snippet}")
        parts.append("")

    memories = search_memories(current_query, user_id=user_id, k=max_memories)
    if memories:
        parts.append("Relevant long-term memories:")
        for mem in memories:
            cat = mem.get("category", "fact")
            parts.append(f"  - [{cat}] {mem['content']}")
        parts.append("")

    return "\n".join(parts)
