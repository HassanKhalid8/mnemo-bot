"""
One-shot move: local SQLite file -> hosted Postgres (Supabase).
---------------------------------------------------------------
Before accounts existed, everything in `chat_history.db` belonged to whoever
was sitting at the machine. Now every row needs an owner, so this copies the
old conversations, messages and memories into the hosted database and files
them under one account.

    # 1. put the Supabase connection string in .env as DATABASE_URL
    # 2. start the app, create your account through the sign-up form
    # 3. then, once:
    python migrate_to_supabase.py you@example.com

Safe to think about, hard to regret:

  * it only ever reads the SQLite file — nothing local is changed or deleted
  * it refuses to run twice for the same account unless you pass --again, so a
    re-run can't silently double your history
  * pass --dry-run first to see exactly what it would copy

The destination account has to exist already; this script deliberately can't
create one, because that would mean handling a password here.
"""

import argparse
import os
import sqlite3
import sys

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

SQLITE_PATH = os.path.join(HERE, "chat_history.db")

# db.py decides its backend from DATABASE_URL at import time, so the check has
# to happen before the import.
if not os.environ.get("DATABASE_URL", "").strip():
    sys.exit(
        "DATABASE_URL isn't set, so db.py would still be pointing at SQLite —\n"
        "there'd be nothing to migrate *to*. Put your Supabase connection\n"
        "string in .env first (see .env.example)."
    )

import db  # noqa: E402  (import must follow the DATABASE_URL check)


def read_sqlite():
    """Everything worth carrying over, straight out of the old file."""
    if not os.path.exists(SQLITE_PATH):
        sys.exit(f"No SQLite database at {SQLITE_PATH} — nothing to migrate.")

    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
        # Older files predate some of these columns; select only what's there.
        optional = [c for c in ("system_prompt", "model", "pinned", "summary",
                                "summarized_through") if c in columns]
        selected = ", ".join(["id", "title", "created_at", "updated_at"] + optional)

        conversations = [dict(r) for r in conn.execute(
            f"SELECT {selected} FROM conversations ORDER BY id"
        )]
        messages = [dict(r) for r in conn.execute(
            "SELECT conversation_id, role, text, created_at FROM messages ORDER BY id"
        )]
        memories = [dict(r) for r in conn.execute(
            "SELECT text, created_at FROM memories ORDER BY id"
        )]
    finally:
        conn.close()

    return conversations, messages, memories


def migrate(email, dry_run=False, again=False):
    user = db.get_user_by_email(email)
    if not user:
        sys.exit(
            f"No account for {email} in the hosted database.\n"
            "Start the app, sign up with that email, then run this again."
        )
    user_id = user["id"]

    conversations, messages, memories = read_sqlite()
    by_conversation = {}
    for message in messages:
        by_conversation.setdefault(message["conversation_id"], []).append(message)

    print(f"Found in {os.path.basename(SQLITE_PATH)}:")
    print(f"  {len(conversations)} conversations")
    print(f"  {len(messages)} messages")
    print(f"  {len(memories)} memories")
    print(f"Destination: {email} (user id {user_id}) on {db.backend()}\n")

    existing = db.list_conversations(user_id)
    if existing and not again:
        sys.exit(
            f"{email} already has {len(existing)} conversations in the hosted\n"
            "database. Refusing to run in case this is a second attempt and\n"
            "you'd end up with everything twice. Pass --again to go ahead."
        )

    if dry_run:
        for conversation in conversations:
            count = len(by_conversation.get(conversation["id"], []))
            print(f"  would copy “{conversation['title']}” ({count} messages)")
        print(f"\n  would copy {len(memories)} memories")
        print("\nDry run — nothing was written.")
        return

    db.init_db()

    copied_messages = 0
    for conversation in conversations:
        old_id = conversation.pop("id")
        turns = by_conversation.get(old_id, [])

        new_id = db.create_conversation(user_id, conversation.get("title") or "New chat")
        # Settings the sidebar and the model picker care about. created_at and
        # updated_at are set by create_conversation; the originals are restored
        # below so the sidebar's ordering survives the move.
        db.update_conversation(
            new_id, user_id,
            system_prompt=conversation.get("system_prompt"),
            model=conversation.get("model"),
            pinned=conversation.get("pinned") or 0,
            summary=conversation.get("summary"),
            summarized_through=conversation.get("summarized_through") or 0,
        )

        with db.connect() as conn:
            for turn in turns:
                conn.execute(
                    "INSERT INTO messages (conversation_id, role, text, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (new_id, turn["role"], turn["text"], turn["created_at"]),
                )
            conn.execute(
                "UPDATE conversations SET created_at = ?, updated_at = ? WHERE id = ?",
                (conversation["created_at"], conversation["updated_at"], new_id),
            )

        copied_messages += len(turns)
        print(f"  copied “{conversation.get('title')}” ({len(turns)} messages)")

    copied_memories = 0
    for memory in memories:
        if db.add_memory(user_id, memory["text"]):
            copied_memories += 1

    print(f"\nDone. {len(conversations)} conversations, {copied_messages} messages "
          f"and {copied_memories} memories now belong to {email}.")
    print("The SQLite file is untouched — keep it around until you've checked "
          "everything looks right in the app.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("email", help="the account to file the old history under")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be copied, write nothing")
    parser.add_argument("--again", action="store_true",
                        help="migrate even though this account already has chats")
    args = parser.parse_args()

    migrate(args.email, dry_run=args.dry_run, again=args.again)
