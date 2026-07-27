"""
Durable storage for the chat history.
-------------------------------------
The assignment's "memory" is the in-memory list in `app.py`. This module is
the layer *underneath* it: every turn that gets appended to that list is also
written to a small SQLite file, so closing the browser — or stopping the
server entirely — no longer throws the conversation away. On the next start
the list is simply re-hydrated from here.

Two tables, nothing exotic:
    conversations — one row per chat thread (id, title, timestamps)
    messages      — one row per turn, pointing back at its conversation

SQLite ships with Python, so this adds zero dependencies.
"""

import os
import re
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL DEFAULT 'New chat',
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT    NOT NULL,
    text            TEXT    NOT NULL,
    created_at      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (conversation_id, id);

-- Long-term memory: facts worth carrying across conversations, not just
-- within one. These get injected into the system prompt on every call.
CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text            TEXT    NOT NULL UNIQUE,
    source_conversation_id INTEGER,
    created_at      REAL    NOT NULL
);
"""

# Columns added after the first version shipped. Applied on every startup so an
# existing chat_history.db upgrades in place instead of needing to be deleted.
MIGRATIONS = [
    ("conversations", "system_prompt", "TEXT"),
    ("conversations", "model", "TEXT"),
    ("conversations", "pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "summary", "TEXT"),
    ("conversations", "summarized_through", "INTEGER NOT NULL DEFAULT 0"),
]


def connect():
    """A fresh connection per call — Flask's dev server is threaded and SQLite
    connections aren't safe to share across threads."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# --- conversations ---------------------------------------------------------

def create_conversation(title="New chat"):
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, now, now),
        )
        return cur.lastrowid


CONVERSATION_FIELDS = (
    "id, title, created_at, updated_at, system_prompt, model, pinned, "
    "summary, summarized_through"
)


def list_conversations():
    """Pinned first, then newest, with a message count and preview for the sidebar."""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT c.{CONVERSATION_FIELDS.replace(', ', ', c.')},
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                       AS message_count,
                   (SELECT m.text FROM messages m
                     WHERE m.conversation_id = c.id
                     ORDER BY m.id DESC LIMIT 1) AS preview
              FROM conversations c
             ORDER BY c.pinned DESC, c.updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conversation_id):
    with connect() as conn:
        row = conn.execute(
            f"SELECT {CONVERSATION_FIELDS} FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    return dict(row) if row else None


def update_conversation(conversation_id, **fields):
    """Patch any of the settings columns. Unknown keys are ignored so this can
    be fed straight from a request body."""
    allowed = {"title", "system_prompt", "model", "pinned", "summary",
               "summarized_through"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return

    assignments = ", ".join(f"{k} = ?" for k in updates)
    with connect() as conn:
        conn.execute(
            f"UPDATE conversations SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), time.time(), conversation_id),
        )


def rename_conversation(conversation_id, title):
    with connect() as conn:
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title.strip() or "New chat", time.time(), conversation_id),
        )


def touch_conversation(conversation_id):
    with connect() as conn:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (time.time(), conversation_id),
        )


def delete_conversation(conversation_id):
    with connect() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def delete_all_conversations():
    with connect() as conn:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM conversations")


# --- messages --------------------------------------------------------------

def add_message(conversation_id, role, text):
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, text, created_at) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, role, text, now),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        return {"id": cur.lastrowid, "role": role, "text": text, "created_at": now}


def get_messages(conversation_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, text, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_messages(conversation_id):
    """Empty the thread but keep the thread itself — its title, persona, model
    and place in the sidebar all survive."""
    with connect() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute(
            "UPDATE conversations SET updated_at = ?, summary = NULL, "
            "summarized_through = 0 WHERE id = ?",
            (time.time(), conversation_id),
        )


def truncate_from(conversation_id, message_id):
    """Delete `message_id` and everything after it. Backs 'regenerate' and
    'edit this turn', which both mean "rewind the thread to here and replay"."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id >= ?",
            (conversation_id, message_id),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (time.time(), conversation_id),
        )


# --- long-term memory ------------------------------------------------------

def add_memory(text, source_conversation_id=None):
    """Store a durable fact. UNIQUE on text means re-learning the same thing is
    a no-op rather than a duplicate row."""
    text = text.strip()
    if not text:
        return None
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO memories (text, source_conversation_id, created_at) "
            "VALUES (?, ?, ?)",
            (text, source_conversation_id, time.time()),
        )
        return cur.lastrowid if cur.rowcount else None


def list_memories():
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, text, source_conversation_id, created_at "
            "FROM memories ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_memory(memory_id):
    with connect() as conn:
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))


def clear_memories():
    with connect() as conn:
        conn.execute("DELETE FROM memories")


# --- search ----------------------------------------------------------------

def _snippet(text, query, radius=90):
    """Return a window of `text` centred on the first match, with the match
    wrapped in «» markers the frontend turns into <mark> tags."""
    lowered = text.lower()
    pos = lowered.find(query.lower())
    if pos == -1:
        return text[: radius * 2] + ("…" if len(text) > radius * 2 else "")

    start = max(0, pos - radius)
    end = min(len(text), pos + len(query) + radius)
    body = text[start:end]

    # Re-locate the match inside the trimmed window and mark it.
    rel = pos - start
    body = body[:rel] + "«" + body[rel:rel + len(query)] + "»" + body[rel + len(query):]

    return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")


def search_messages(query, limit=80):
    """Case-insensitive substring search across every stored message."""
    query = query.strip()
    if not query:
        return []

    # Escape LIKE wildcards so a literal % or _ in the query behaves.
    escaped = re.sub(r"([%_\\])", r"\\\1", query)

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT m.id,
                   m.conversation_id,
                   m.role,
                   m.text,
                   m.created_at,
                   c.title AS conversation_title
              FROM messages m
              JOIN conversations c ON c.id = m.conversation_id
             WHERE m.text LIKE ? ESCAPE '\\'
             ORDER BY m.created_at DESC
             LIMIT ?
            """,
            (f"%{escaped}%", limit),
        ).fetchall()

    return [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "conversation_title": r["conversation_title"],
            "role": r["role"],
            "created_at": r["created_at"],
            "snippet": _snippet(r["text"], query),
        }
        for r in rows
    ]


def stats():
    with connect() as conn:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM conversations) AS conversations, "
            "       (SELECT COUNT(*) FROM messages)      AS messages, "
            "       (SELECT COUNT(*) FROM memories)      AS memories"
        ).fetchone()
    return dict(row)
