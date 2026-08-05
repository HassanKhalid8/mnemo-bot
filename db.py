"""
Durable storage for accounts and chat history.
----------------------------------------------
The assignment's "memory" is the in-memory list in `app.py`. This module is
the layer *underneath* it: every turn appended to that list is also written
here, so closing the browser — or stopping the server entirely — no longer
throws the conversation away. On the next start the list is re-hydrated.

Everything below the `users` table is **owned by a user**. Conversations,
messages and long-term memories all hang off a `user_id`, and every query in
this file filters on it, so two people using the same deployment can never see
each other's history. Signed-out visitors own nothing: their chat is held in
the browser and never reaches this module at all.

Two backends, picked automatically:

    DATABASE_URL set   ->  Postgres (psycopg).  Supabase in production: a
                           hosted deployment has no durable filesystem, so a
                           local file would be wiped between requests.
    DATABASE_URL unset ->  SQLite file next to this module. Zero setup,
                           perfect for local development.

The schema is identical either way; only the dialect differs. All SQL in this
file is written with SQLite-style `?` placeholders and translated on the way
out, so there is exactly one copy of every query.
"""

import os
import re
import sqlite3
import time

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = bool(DATABASE_URL)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history.db")

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

    # Neon/Supabase hand out URLs starting with "postgres://", which psycopg 3
    # doesn't accept. Normalise it.
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

# `AUTOINC` differs; everything else in the schema is common SQL.
_AUTOINC = "BIGSERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
_REAL = "DOUBLE PRECISION" if IS_POSTGRES else "REAL"

SCHEMA = [
    # Accounts. `email` is stored already lower-cased and trimmed by the
    # caller, so UNIQUE is enough to stop Alex@x.com and alex@x.com being two
    # different people. Only the hash of the password is ever kept.
    f"""
    CREATE TABLE IF NOT EXISTS users (
        id            {_AUTOINC},
        email         TEXT    NOT NULL UNIQUE,
        name          TEXT,
        password_hash TEXT    NOT NULL,
        created_at    {_REAL} NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS conversations (
        id          {_AUTOINC},
        title       TEXT    NOT NULL DEFAULT 'New chat',
        created_at  {_REAL} NOT NULL,
        updated_at  {_REAL} NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS messages (
        id              {_AUTOINC},
        conversation_id BIGINT  NOT NULL,
        role            TEXT    NOT NULL,
        text            TEXT    NOT NULL,
        created_at      {_REAL} NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_conversation
        ON messages (conversation_id, id)
    """,
    # Long-term memory: facts worth carrying across conversations, not just
    # within one. These get injected into the system prompt on every call.
    # Uniqueness is per user, enforced by an index further down — two people
    # are both allowed to have told it their name.
    f"""
    CREATE TABLE IF NOT EXISTS memories (
        id                     {_AUTOINC},
        text                   TEXT    NOT NULL,
        source_conversation_id BIGINT,
        created_at             {_REAL} NOT NULL
    )
    """,
]

# Columns added after the first version shipped. Applied on every startup so an
# existing database upgrades in place instead of needing to be recreated.
MIGRATIONS = [
    ("conversations", "system_prompt", "TEXT"),
    ("conversations", "model", "TEXT"),
    ("conversations", "pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "summary", "TEXT"),
    ("conversations", "summarized_through", "INTEGER NOT NULL DEFAULT 0"),
    # Ownership. Nullable on purpose: rows created before accounts existed keep
    # a NULL owner, which every query below simply never matches.
    ("conversations", "user_id", "BIGINT"),
    ("memories", "user_id", "BIGINT"),
]

# Tables that hold user-owned data. Used by the Postgres hardening step below.
_TABLES = ("users", "conversations", "messages", "memories")

# Indexes over columns that MIGRATIONS adds, so they can only be created once
# those columns exist.
POST_MIGRATION_SCHEMA = [
    """
    CREATE INDEX IF NOT EXISTS idx_conversations_user
        ON conversations (user_id, updated_at)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_user_text
        ON memories (user_id, text)
    """,
]


# --- connection plumbing ---------------------------------------------------

class _Cursor:
    """Wraps a DB-API cursor so callers can always write `?` placeholders and
    always read rows as dicts, whichever backend is underneath."""

    def __init__(self, cursor):
        self._cursor = cursor

    @staticmethod
    def _translate(sql):
        if not IS_POSTGRES:
            return sql
        # `?` -> `%s`, but never inside a quoted string (the LIKE ESCAPE
        # clause in search_messages contains a literal '\').
        out, in_string = [], False
        for char in sql:
            if char == "'":
                in_string = not in_string
            if char == "?" and not in_string:
                out.append("%s")
            else:
                out.append(char)
        return "".join(out)

    def execute(self, sql, params=()):
        self._cursor.execute(self._translate(sql), tuple(params))
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(r) for r in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _Connection:
    """Context manager that commits on success and rolls back on error.

    sqlite3's own connection context manager does this already; psycopg's does
    too, but they disagree about closing. This wrapper makes both behave the
    same and guarantees the connection is released either way — important on
    serverless, where leaked connections exhaust the pool fast.
    """

    def __init__(self):
        if IS_POSTGRES:
            # prepare_threshold=None turns off psycopg's automatic prepared
            # statements. After the same query runs a few times on one
            # connection it would otherwise PREPARE it — which Supabase's
            # transaction pooler rejects, because the next statement may land
            # on a different backend that has never heard of that plan. It
            # only bites where we loop one query on a single connection
            # (importing a guest chat, or the migration script), and it bites
            # as a hard error, so it is switched off rather than tiptoed around.
            self._conn = psycopg.connect(
                DATABASE_URL, row_factory=dict_row, prepare_threshold=None
            )
        else:
            self._conn = sqlite3.connect(DB_PATH)
            self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        return _Cursor(self._conn.cursor()).execute(sql, params)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


def connect():
    """A fresh connection per call. Flask's dev server is threaded and SQLite
    connections aren't safe to share across threads; on serverless every
    invocation is isolated anyway."""
    return _Connection()


def init_db():
    with connect() as conn:
        for statement in SCHEMA:
            conn.execute(statement)

        # Cache per table, so a future migration on `messages` or `memories`
        # doesn't get checked against the wrong table's columns.
        seen = {}
        for table, column, decl in MIGRATIONS:
            if table not in seen:
                seen[table] = _columns(conn, table)
            if column not in seen[table]:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                seen[table].add(column)

        # Version 1 made `memories.text` globally unique, which now stops a
        # second user from ever learning a fact the first one already had —
        # their INSERT is silently ignored. Postgres can drop the constraint in
        # place; SQLite has no way to drop one, so the table gets rebuilt.
        if IS_POSTGRES:
            conn.execute("ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_text_key")
        elif _has_legacy_text_unique(conn):
            _rebuild_memories(conn)

        for statement in POST_MIGRATION_SCHEMA:
            conn.execute(statement)

        if IS_POSTGRES:
            _harden_postgres(conn)


def _harden_postgres(conn):
    """Close the hole Supabase's linter flags as `rls_disabled_in_public`.

    A Supabase project publishes every table in the `public` schema through a
    REST API at `https://<ref>.supabase.co/rest/v1/`, reachable with the
    project's *anonymous* key — a key that is designed to be public and shipped
    to browsers. What stops the world from reading `users` through it is not
    secrecy; it is Row-Level Security. With RLS off, that endpoint hands anyone
    holding the anon key full SELECT/INSERT/UPDATE/DELETE on every row.

    This app never touches that REST API. It connects straight to Postgres as
    the `postgres` role over DATABASE_URL, and that role has BYPASSRLS, so both
    statements below are invisible to every query in this module:

      1. ENABLE ROW LEVEL SECURITY — with zero policies defined, the default is
         deny. `anon` and `authenticated` now match no rows at all.
      2. REVOKE — belt and braces. Even if a policy were added later by
         accident, neither role holds a privilege to exercise it.

    Both are idempotent, so this runs on every start. New deployment, new
    Supabase project, same protection — nobody has to remember to click it in
    the dashboard.

    Each step is checked before it is applied, in the same guard-then-alter
    style as the column migrations above. That is not just tidiness: `init_db`
    runs at import in app.py, which on serverless means *every cold start*, and
    ALTER TABLE takes an ACCESS EXCLUSIVE lock even when it changes nothing.
    Once hardened, the whole function is two catalog reads and no locks.
    """
    unprotected = [
        row["relname"] for row in conn.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT c.relrowsecurity "
            f"AND c.relname IN ({', '.join('?' * len(_TABLES))})",
            _TABLES,
        ).fetchall()
    ]
    for table in unprotected:
        conn.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")

    # `anon` and `authenticated` exist on Supabase but not on a plain Postgres
    # (or Neon), where REVOKE against a missing role is a hard error — and
    # has_table_privilege() on an unknown role raises rather than returning
    # false. So the roles are looked up first, and only the grants that are
    # actually still held get revoked.
    roles = [
        row["rolname"] for row in conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')"
        ).fetchall()
    ]
    for role in roles:
        for table in _TABLES:
            still_granted = conn.execute(
                "SELECT has_table_privilege(?, ?, 'SELECT, INSERT, UPDATE, DELETE') "
                "AS granted",
                (role, f"public.{table}"),
            ).fetchone()["granted"]
            if still_granted:
                conn.execute(f"REVOKE ALL ON public.{table} FROM {role}")


def _has_legacy_text_unique(conn):
    """True if this SQLite file still carries the old table-level
    UNIQUE(text). Its auto-index is the only trace of it."""
    for index in conn.execute("PRAGMA index_list(memories)").fetchall():
        if not index.get("unique"):
            continue
        columns = [
            row["name"] for row in
            conn.execute(f"PRAGMA index_info('{index['name']}')").fetchall()
        ]
        if columns == ["text"]:
            return True
    return False


def _rebuild_memories(conn):
    """Copy `memories` into a table declared without UNIQUE(text) and swap it
    in. Ids and every row are preserved; only the constraint is dropped."""
    conn.execute("DROP TABLE IF EXISTS memories_rebuilt")
    conn.execute(f"""
        CREATE TABLE memories_rebuilt (
            id                     {_AUTOINC},
            text                   TEXT    NOT NULL,
            source_conversation_id BIGINT,
            created_at             {_REAL} NOT NULL,
            user_id                BIGINT
        )
    """)
    conn.execute(
        "INSERT INTO memories_rebuilt "
        "(id, text, source_conversation_id, created_at, user_id) "
        "SELECT id, text, source_conversation_id, created_at, user_id FROM memories"
    )
    conn.execute("DROP TABLE memories")
    conn.execute("ALTER TABLE memories_rebuilt RENAME TO memories")


def _columns(conn, table):
    """Column names for a table, however the backend exposes them."""
    if IS_POSTGRES:
        rows = conn.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_name = ?",
            (table,),
        ).fetchall()
    else:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _returning_id(conn, sql, params):
    """INSERT ... RETURNING id. Supported by Postgres and by SQLite 3.35+,
    which is comfortably older than any Python that runs this app."""
    row = conn.execute(sql + " RETURNING id", params).fetchone()
    return row["id"] if row else None


# --- users -----------------------------------------------------------------
# Only ever handed a *hash*; this module knows nothing about how passwords are
# checked, and never returns the hash to anything but the login check.

USER_FIELDS = "id, email, name, created_at"


def normalise_email(email):
    return (email or "").strip().lower()


def create_user(email, password_hash, name=None):
    """Returns the new row, or None if that email is already taken."""
    email = normalise_email(email)
    if get_user_by_email(email):
        return None
    with connect() as conn:
        user_id = _returning_id(
            conn,
            "INSERT INTO users (email, name, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (email, (name or "").strip() or None, password_hash, time.time()),
        )
    return get_user(user_id)


def get_user(user_id):
    with connect() as conn:
        return conn.execute(
            f"SELECT {USER_FIELDS} FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def get_user_by_email(email):
    with connect() as conn:
        return conn.execute(
            f"SELECT {USER_FIELDS} FROM users WHERE email = ?", (normalise_email(email),)
        ).fetchone()


def get_credentials(email):
    """The one query allowed to read the hash — used by the login check."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, password_hash FROM users WHERE email = ?",
            (normalise_email(email),),
        ).fetchone()


def set_user_name(user_id, name):
    with connect() as conn:
        conn.execute(
            "UPDATE users SET name = ? WHERE id = ?",
            ((name or "").strip() or None, user_id),
        )


# --- conversations ---------------------------------------------------------

CONVERSATION_FIELDS = (
    "id, title, created_at, updated_at, system_prompt, model, pinned, "
    "summary, summarized_through"
)


def create_conversation(user_id, title="New chat"):
    now = time.time()
    with connect() as conn:
        return _returning_id(
            conn,
            "INSERT INTO conversations (user_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, title, now, now),
        )


def list_conversations(user_id):
    """Pinned first, then newest, with a message count and preview for the sidebar."""
    with connect() as conn:
        return conn.execute(
            f"""
            SELECT c.{CONVERSATION_FIELDS.replace(', ', ', c.')},
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                       AS message_count,
                   (SELECT m.text FROM messages m
                     WHERE m.conversation_id = c.id
                     ORDER BY m.id DESC LIMIT 1) AS preview
              FROM conversations c
             WHERE c.user_id = ?
             ORDER BY c.pinned DESC, c.updated_at DESC
            """,
            (user_id,),
        ).fetchall()


def get_conversation(conversation_id, user_id):
    """None when the row doesn't exist *or* belongs to somebody else — callers
    treat both as a 404, which is also what stops one user probing another's
    conversation ids."""
    with connect() as conn:
        return conn.execute(
            f"SELECT {CONVERSATION_FIELDS} FROM conversations "
            "WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()


def update_conversation(conversation_id, user_id, **fields):
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
            f"UPDATE conversations SET {assignments}, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (*updates.values(), time.time(), conversation_id, user_id),
        )


def rename_conversation(conversation_id, user_id, title):
    update_conversation(conversation_id, user_id, title=title.strip() or "New chat")


def touch_conversation(conversation_id, user_id):
    with connect() as conn:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ? AND user_id = ?",
            (time.time(), conversation_id, user_id),
        )


def delete_conversation(conversation_id, user_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM messages WHERE conversation_id IN "
            "(SELECT id FROM conversations WHERE id = ? AND user_id = ?)",
            (conversation_id, user_id),
        )
        conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )


def delete_all_conversations(user_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM messages WHERE conversation_id IN "
            "(SELECT id FROM conversations WHERE user_id = ?)",
            (user_id,),
        )
        conn.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))


def import_conversation(user_id, title, turns):
    """Adopt a transcript the browser was holding — what happens when someone
    chats signed-out and then makes an account. Returns the new id."""
    now = time.time()
    conversation_id = create_conversation(user_id, title)
    with connect() as conn:
        for offset, turn in enumerate(turns):
            conn.execute(
                "INSERT INTO messages (conversation_id, role, text, created_at) "
                "VALUES (?, ?, ?, ?)",
                # Nudge each timestamp forward so the transcript keeps its order
                # even if the rows are read back by created_at.
                (conversation_id, turn["role"], turn["text"], now + offset * 0.001),
            )
    return conversation_id


# --- messages --------------------------------------------------------------
# Messages are reached only through a conversation the caller has already been
# proved to own, so these take an id rather than re-checking ownership.

def add_message(conversation_id, role, text):
    now = time.time()
    with connect() as conn:
        message_id = _returning_id(
            conn,
            "INSERT INTO messages (conversation_id, role, text, created_at) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, role, text, now),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
    return {"id": message_id, "role": role, "text": text, "created_at": now}


def get_messages(conversation_id):
    with connect() as conn:
        return conn.execute(
            "SELECT id, role, text, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()


def delete_message(message_id):
    with connect() as conn:
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))


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

def add_memory(user_id, text, source_conversation_id=None):
    """Store a durable fact. UNIQUE on (user_id, text) means re-learning the
    same thing is a no-op rather than a duplicate row."""
    text = text.strip()
    if not text:
        return None

    # Same intent, two dialects: skip the row if that user already knows it.
    columns = "(user_id, text, source_conversation_id, created_at) VALUES (?, ?, ?, ?)"
    if IS_POSTGRES:
        sql = f"INSERT INTO memories {columns} ON CONFLICT (user_id, text) DO NOTHING"
    else:
        sql = f"INSERT OR IGNORE INTO memories {columns}"

    with connect() as conn:
        return _returning_id(
            conn, sql, (user_id, text, source_conversation_id, time.time())
        )


def list_memories(user_id):
    with connect() as conn:
        return conn.execute(
            "SELECT id, text, source_conversation_id, created_at "
            "FROM memories WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()


def delete_memory(memory_id, user_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)
        )


def clear_memories(user_id):
    with connect() as conn:
        conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))


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

    rel = pos - start
    body = body[:rel] + "«" + body[rel:rel + len(query)] + "»" + body[rel + len(query):]

    return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")


def search_messages(user_id, query, limit=80):
    """Case-insensitive substring search across this user's stored messages."""
    query = query.strip()
    if not query:
        return []

    # Escape LIKE wildcards so a literal % or _ in the query behaves.
    escaped = re.sub(r"([%_\\])", r"\\\1", query)

    # Postgres ILIKE is case-insensitive; SQLite's LIKE already is for ASCII.
    operator = "ILIKE" if IS_POSTGRES else "LIKE"

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT m.id,
                   m.conversation_id,
                   m.role,
                   m.text,
                   m.created_at,
                   c.title AS conversation_title
              FROM messages m
              JOIN conversations c ON c.id = m.conversation_id
             WHERE c.user_id = ?
               AND m.text {operator} ? ESCAPE '\\'
             ORDER BY m.created_at DESC
             LIMIT ?
            """,
            (user_id, f"%{escaped}%", limit),
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


def stats(user_id):
    with connect() as conn:
        return conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM conversations WHERE user_id = ?)
                       AS conversations,
                   (SELECT COUNT(*) FROM messages m
                      JOIN conversations c ON c.id = m.conversation_id
                     WHERE c.user_id = ?) AS messages,
                   (SELECT COUNT(*) FROM memories WHERE user_id = ?) AS memories
            """,
            (user_id, user_id, user_id),
        ).fetchone()


def backend():
    """Which store is live — surfaced at /api/history for sanity checking a
    deployment."""
    return "postgres" if IS_POSTGRES else "sqlite"
