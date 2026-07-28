"""
Mnemo — an AI chatbot with memory
------------------------------------------------
A Flask server that talks to Google's Gemini API (via the official
google-genai Python SDK) and keeps the conversation "alive" using a plain
in-memory list — the core requirement of the project.

Layered on top of that core:

  * **Accounts** — signed-out visitors get a working chatbot whose transcript
    lives in their browser and is never written down. Making an account turns
    that on: from then on every thread, and everything the bot learns, is
    stored against *their* user id and comes back when they sign in again.
  * **Persistence** (`db.py`) — every turn appended to the in-memory list is
    mirrored into the database, so history survives closing the tab *and*
    restarting the server.
  * **Multiple conversations + search**, each with its own persona and model.
  * **Long-term memory** — durable facts extracted in the background and
    injected into the system prompt of *every* conversation, so the bot
    remembers you across threads, not just within one.
  * **Context management** — live token counting, and once a thread gets long
    the oldest turns are collapsed into a summary instead of being resent
    verbatim forever.
  * **Incognito** — a RAM-only thread that never touches disk and is purged
    when you leave it.
  * **Text to speech** (`tts.py`) — free Microsoft neural voices, no quota.

Run it with:
    python app.py

Then open http://localhost:5000 in a browser.
"""

import datetime
import json
import os
import re
import threading
import uuid
from functools import wraps

from flask import (Flask, Response, jsonify, render_template, request,
                   session, stream_with_context)
from google import genai
from google.genai import errors, types
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

import db
import tts

# pulls GEMINI_API_KEY (and optional overrides) from the .env next to this
# file, so the server works no matter which directory you launch it from
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

app = Flask(__name__)

# --- 0. Sessions -----------------------------------------------------------
# Who you are is kept in Flask's signed cookie: no server-side session store to
# run, which matters on serverless where there's nowhere to put one. The
# signature is only as good as this key, so a real deployment must set it —
# and must keep it stable, since changing it signs everybody out.
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    print("WARNING: SECRET_KEY is not set — using an insecure development key. "
          "Set one before deploying, or sessions can be forged.")
app.secret_key = SECRET_KEY or "dev-only-insecure-key-do-not-deploy-with-this"

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # http://localhost has no TLS, so a Secure cookie would never be sent back.
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")
                               or os.environ.get("SECURE_COOKIES")),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
)

# --- 1. Connect to the LLM using the official SDK -------------------------
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    print("WARNING: GEMINI_API_KEY is not set. Copy .env.example to .env "
          "and add your key before chatting.")

client = genai.Client(api_key=API_KEY) if API_KEY else None

# "gemini-flash-latest" is a rolling alias Google keeps pointed at its
# current-generation Flash model, so this doesn't need to be hand-updated
# every time a new Gemini version ships.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

# Picking a model per conversation. Aliases, so they don't go stale.
MODELS = [
    {"id": "gemini-flash-latest",      "name": "Flash",      "blurb": "Fast and capable — the default"},
    {"id": "gemini-flash-lite-latest", "name": "Flash Lite", "blurb": "Fastest, most generous free quota"},
    {"id": "gemini-pro-latest",        "name": "Pro",        "blurb": "Best reasoning — barely any free-tier quota, expect 429s"},
]
MODEL_IDS = {m["id"] for m in MODELS}

# The model used for the cheap background jobs (fact extraction, summarising).
# Deliberately the smallest one — these run often and don't need much.
UTILITY_MODEL = os.environ.get("GEMINI_UTILITY_MODEL", "gemini-flash-lite-latest")

SYSTEM_PROMPT = (
    "You are Mnemo, a helpful and concise assistant. Your name comes from "
    "Mnemosyne, the Greek personification of memory — remembering is what you "
    "are for. Stay conversational, and use what the user has told you earlier "
    "without making a performance of it. Use Markdown when it aids readability."
)

# Once a thread's transcript passes this many tokens, the oldest turns get
# folded into a summary instead of being resent verbatim every call. Modern
# Gemini context windows are ~1M tokens, so this is about keeping calls cheap
# and fast, not about avoiding a hard error.
SUMMARY_TRIGGER_TOKENS = int(os.environ.get("SUMMARY_TRIGGER_TOKENS", "8000"))
KEEP_RECENT_MESSAGES = int(os.environ.get("KEEP_RECENT_MESSAGES", "8"))

# Serverless platforms freeze or discard the instance the moment a handler
# returns, which kills detached threads and empties any module-level cache.
# Vercel sets VERCEL=1 itself; set SERVERLESS=1 by hand on other platforms.
SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("SERVERLESS"))

# --- 2. Session state: in-memory lists -------------------------------------
# This is still the "memory" for the app: a plain Python list per conversation
# holding {"role": "user"|"model", "text": "..."} dicts, converted into the
# SDK's typed Content objects only at call time (see build_contents below).
# Gemini's API is stateless per request, so *we* resend the transcript every
# time — that list is what makes the model look like it remembers.
#
# Persisted threads lazily rehydrate from SQLite; incognito threads live here
# and nowhere else, and are dropped entirely when the user leaves them.
conversation_histories = {}
incognito_histories = {}

db.init_db()

_limit_cache = {}
_limit_lock = threading.Lock()


def context_limit(model):
    """Input token ceiling for a model, asked once and cached."""
    with _limit_lock:
        if model in _limit_cache:
            return _limit_cache[model]
    limit = 1_000_000
    try:
        info = client.models.get(model=model)
        limit = getattr(info, "input_token_limit", None) or limit
    except Exception:
        pass  # offline or unknown model — the generous default is fine
    with _limit_lock:
        _limit_cache[model] = limit
    return limit


def history_for(conversation_id):
    """Return the in-memory list for a conversation, rehydrating from the
    database the first time it's touched after a restart."""
    if conversation_id not in conversation_histories:
        conversation_histories[conversation_id] = [
            {"role": m["role"], "text": m["text"], "id": m["id"]}
            for m in db.get_messages(conversation_id)
        ]
    return conversation_histories[conversation_id]


def build_contents(history, skip=0):
    """Turn our plain-dict history into the list of types.Content the SDK
    expects. `skip` drops leading turns already covered by a stored summary."""
    return [
        types.Content(role=turn["role"], parts=[types.Part(text=turn["text"])])
        for turn in history[skip:]
    ]


def build_system_prompt(conversation=None, user_id=None):
    """The persona, plus anything we've learned about this user, plus a summary
    of turns that have been trimmed out of the transcript.

    `user_id` of None means nobody is signed in, or this is an incognito
    thread. Either way no stored facts are looked up, so a saved memory can
    never leak into a chat that isn't the owner's."""
    parts = [(conversation or {}).get("system_prompt") or SYSTEM_PROMPT]

    if user_id:
        facts = [m["text"] for m in db.list_memories(user_id)]
        if facts:
            parts.append(
                "Things you have learned about this user in past conversations "
                "(use them naturally; don't recite them back unprompted):\n"
                + "\n".join(f"- {f}" for f in facts)
            )

    summary = (conversation or {}).get("summary")
    if summary:
        parts.append(
            "Summary of the earlier part of this conversation, which has been "
            "trimmed from the transcript below:\n" + summary
        )

    return "\n\n".join(parts)


def derive_title(message, limit=48):
    """First user message, trimmed, becomes the thread title in the sidebar."""
    title = " ".join(message.split())
    if len(title) > limit:
        title = title[:limit].rsplit(" ", 1)[0] + "…"
    return title or "New chat"


def sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def config():
    return jsonify({
        "models": MODELS,
        "default_model": MODEL,
        "default_system_prompt": SYSTEM_PROMPT,
        "has_api_key": bool(API_KEY),
    })


# --- accounts --------------------------------------------------------------
# Everything that touches stored data goes through @login_required, which hands
# the handler the caller's user id as its first argument. There is deliberately
# no way to reach another user's rows: db.py filters on that id in the query
# itself, so a guessed conversation id comes back as a 404 rather than as
# somebody else's chat.

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8


def current_user_id():
    """The signed-in user's id, or None. Read straight from the signed cookie —
    no database round-trip, because a stale id simply matches no rows."""
    return session.get("user_id")


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user_id = current_user_id()
        if not user_id:
            return jsonify({
                "error": "Sign in to use this.",
                "auth_required": True,
            }), 401
        return view(user_id, *args, **kwargs)
    return wrapper


def _sign_in(user):
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    return user


@app.route("/api/auth/me", methods=["GET"])
def whoami():
    user_id = current_user_id()
    user = db.get_user(user_id) if user_id else None
    if user_id and not user:
        # Account deleted out from under a live cookie.
        session.clear()
    return jsonify({"user": user})


@app.route("/api/auth/signup", methods=["POST"])
def signup():
    data = request.get_json(force=True, silent=True) or {}
    email = db.normalise_email(data.get("email"))
    password = data.get("password") or ""
    name = (data.get("name") or "").strip()[:80]

    if not EMAIL_RE.match(email):
        return jsonify({"error": "That doesn't look like an email address."}), 400
    if len(password) < MIN_PASSWORD:
        return jsonify({
            "error": f"Use at least {MIN_PASSWORD} characters for your password."
        }), 400

    # pbkdf2 rather than werkzeug's scrypt default: scrypt wants tens of
    # megabytes per hash, which is a poor fit for a small serverless function.
    user = db.create_user(
        email, generate_password_hash(password, method="pbkdf2:sha256"), name
    )
    if not user:
        return jsonify({"error": "There's already an account with that email."}), 409

    _sign_in(user)
    return jsonify({"user": user}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(force=True, silent=True) or {}
    email = db.normalise_email(data.get("email"))
    password = data.get("password") or ""

    credentials = db.get_credentials(email)
    # One message for both "no such account" and "wrong password", so this
    # can't be used to find out which emails are registered.
    if not credentials or not check_password_hash(credentials["password_hash"], password):
        return jsonify({"error": "Wrong email or password."}), 401

    user = db.get_user(credentials["id"])
    _sign_in(user)
    return jsonify({"user": user})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"status": "signed out"})


# --- conversations ---------------------------------------------------------

@app.route("/api/conversations", methods=["GET"])
@login_required
def list_conversations(user_id):
    return jsonify({"conversations": db.list_conversations(user_id)})


@app.route("/api/conversations", methods=["POST"])
@login_required
def new_conversation(user_id):
    conversation_id = db.create_conversation(user_id)
    conversation_histories[conversation_id] = []
    return jsonify({
        "conversation": db.get_conversation(conversation_id, user_id)
    }), 201


@app.route("/api/conversations/import", methods=["POST"])
@login_required
def import_conversation(user_id):
    """Adopt the transcript a signed-out visitor built up in their browser, so
    signing up doesn't cost them the conversation they were in the middle of."""
    data = request.get_json(force=True, silent=True) or {}
    turns = _clean_turns(data.get("messages"))
    if not turns:
        return jsonify({"error": "Nothing to save."}), 400

    first_user_turn = next((t["text"] for t in turns if t["role"] == "user"), "")
    conversation_id = db.import_conversation(
        user_id, derive_title(first_user_turn), turns
    )
    conversation_histories.pop(conversation_id, None)
    return jsonify({
        "conversation": db.get_conversation(conversation_id, user_id),
        "messages": db.get_messages(conversation_id),
    }), 201


@app.route("/api/conversations/<int:conversation_id>", methods=["GET"])
@login_required
def read_conversation(user_id, conversation_id):
    conversation = db.get_conversation(conversation_id, user_id)
    if not conversation:
        return jsonify({"error": "Conversation not found."}), 404
    return jsonify({
        "conversation": conversation,
        "messages": db.get_messages(conversation_id),
        "history_length": len(history_for(conversation_id)),
    })


@app.route("/api/conversations/<int:conversation_id>", methods=["PATCH"])
@login_required
def patch_conversation(user_id, conversation_id):
    if not db.get_conversation(conversation_id, user_id):
        return jsonify({"error": "Conversation not found."}), 404

    data = request.get_json(force=True, silent=True) or {}
    fields = {}

    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            return jsonify({"error": "Title cannot be empty."}), 400
        fields["title"] = title[:120]

    if "system_prompt" in data:
        prompt = (data["system_prompt"] or "").strip()
        fields["system_prompt"] = prompt[:4000] or None

    if "model" in data:
        model = data["model"] or None
        if model and model not in MODEL_IDS:
            return jsonify({"error": "Unknown model."}), 400
        fields["model"] = model

    if "pinned" in data:
        fields["pinned"] = 1 if data["pinned"] else 0

    db.update_conversation(conversation_id, user_id, **fields)
    return jsonify({"conversation": db.get_conversation(conversation_id, user_id)})


@app.route("/api/conversations/<int:conversation_id>", methods=["DELETE"])
@login_required
def remove_conversation(user_id, conversation_id):
    if not db.get_conversation(conversation_id, user_id):
        return jsonify({"error": "Conversation not found."}), 404
    db.delete_conversation(conversation_id, user_id)
    conversation_histories.pop(conversation_id, None)
    return jsonify({"status": "deleted", "id": conversation_id})


@app.route("/api/conversations", methods=["DELETE"])
@login_required
def remove_all_conversations(user_id):
    for conversation in db.list_conversations(user_id):
        conversation_histories.pop(conversation["id"], None)
    db.delete_all_conversations(user_id)
    return jsonify({"status": "cleared"})


# --- incognito -------------------------------------------------------------
# A thread that exists only in this dict. Nothing is written to SQLite, no
# facts are learned from it, and no stored memories are fed into it — so it
# can't leak into or out of your saved history. Leaving the tab purges it.

@app.route("/api/incognito", methods=["POST"])
@login_required
def start_incognito(user_id):
    incognito_id = uuid.uuid4().hex
    incognito_histories[incognito_id] = []
    return jsonify({"incognito_id": incognito_id}), 201


@app.route("/api/incognito/<incognito_id>", methods=["DELETE"])
@login_required
def end_incognito(user_id, incognito_id):
    purged = len(incognito_histories.pop(incognito_id, []))
    return jsonify({"status": "purged", "messages_discarded": purged})


# --- chat ------------------------------------------------------------------

# A signed-out visitor's transcript arrives with every request instead of
# living on the server, so these caps are what stops one from arriving with a
# megabyte of "history" attached.
GUEST_MAX_TURNS = 60
GUEST_MAX_CHARS = 12000


def _clean_turns(turns):
    """Validate a transcript supplied by the browser. Anything malformed is
    dropped rather than trusted — this is user input, not our own data."""
    cleaned = []
    for turn in (turns or [])[-GUEST_MAX_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = "user" if turn.get("role") == "user" else "model"
        text = (turn.get("text") or "").strip()[:GUEST_MAX_CHARS]
        if text:
            cleaned.append({"role": role, "text": text})
    return cleaned


@app.route("/api/chat", methods=["POST"])
def chat():
    """Append the user's message, stream the model's reply back token by token,
    then append that reply to the history payload too.

    Responds with Server-Sent Events so the UI can render the answer as it
    arrives instead of staring at a spinner.

    Signed out, this still works — the browser sends the transcript it is
    holding as `guest_history`, nothing is written down, and no long-term
    memories are read or learned.

    Optional body fields:
      guest_history       — signed-out only: the transcript so far
      incognito_id        — chat in a RAM-only thread instead of a saved one
      regenerate_from     — assistant message id: rewind to it and re-answer
      edit_message_id     — user message id: rewind to it and replace its text
      remember            — whether to extract long-term facts from this turn
    """
    data = request.get_json(force=True, silent=True) or {}
    user_message = (data.get("message") or "").strip()
    conversation_id = data.get("conversation_id")
    incognito_id = data.get("incognito_id")
    regenerate_from = data.get("regenerate_from")
    edit_message_id = data.get("edit_message_id")
    remember = bool(data.get("remember", True))
    user_id = current_user_id()

    if not API_KEY:
        return jsonify({"error": "Server has no GEMINI_API_KEY configured."}), 500

    # ---- guest path: the browser is the only place this thread exists -----
    if not user_id:
        if not user_message:
            return jsonify({"error": "Message cannot be empty."}), 400
        history = _clean_turns(data.get("guest_history"))
        history.append({"role": "user", "text": user_message, "id": None})
        return stream_reply(
            history=history,
            conversation=None,
            conversation_id=None,
            user_id=None,
            ephemeral=True,
            model=MODEL,
            user_row=None,
            remember=False,
        )

    # ---- incognito path: no database, no memories, no titles --------------
    if incognito_id:
        history = incognito_histories.get(incognito_id)
        if history is None:
            return jsonify({"error": "That incognito session has been purged."}), 410
        if not user_message:
            return jsonify({"error": "Message cannot be empty."}), 400

        history.append({"role": "user", "text": user_message, "id": None})
        return stream_reply(
            history=history,
            conversation=None,
            conversation_id=None,
            user_id=None,          # incognito must not see stored memories
            ephemeral=True,
            model=MODEL,
            user_row=None,
            remember=False,
        )

    # ---- saved path -------------------------------------------------------
    if not conversation_id or not db.get_conversation(conversation_id, user_id):
        if not user_message:
            return jsonify({"error": "Message cannot be empty."}), 400
        conversation_id = db.create_conversation(user_id, derive_title(user_message))
        conversation_histories[conversation_id] = []
        created_new = True
    else:
        conversation_id = int(conversation_id)
        created_new = False

    history = history_for(conversation_id)

    # Rewind for regenerate / edit: drop the target message and everything
    # after it, from both the list and the table, then replay.
    rewind_to = regenerate_from or edit_message_id
    if rewind_to:
        rewind_to = int(rewind_to)
        index = next((i for i, t in enumerate(history) if t.get("id") == rewind_to), None)
        if index is None:
            return jsonify({"error": "That message is no longer in this conversation."}), 404
        db.truncate_from(conversation_id, rewind_to)
        del history[index:]
        # A stored summary describes turns that may no longer exist.
        db.update_conversation(conversation_id, user_id, summary=None,
                               summarized_through=0)

    if regenerate_from:
        if not history or history[-1]["role"] != "user":
            return jsonify({"error": "Nothing to regenerate from."}), 400
        user_row = None
    else:
        if not user_message:
            return jsonify({"error": "Message cannot be empty."}), 400
        conversation = db.get_conversation(conversation_id, user_id)
        if not created_new and not history and conversation["title"] == "New chat":
            db.update_conversation(conversation_id, user_id,
                                   title=derive_title(user_message))

        # 3. Append the new user turn to the history payload (memory + disk)
        user_row = db.add_message(conversation_id, "user", user_message)
        history.append({"role": "user", "text": user_message, "id": user_row["id"]})

    conversation = db.get_conversation(conversation_id, user_id)
    return stream_reply(
        history=history,
        conversation=conversation,
        conversation_id=conversation_id,
        user_id=user_id,
        ephemeral=False,
        model=conversation.get("model") or MODEL,
        user_row=user_row,
        remember=remember,
        created_new=created_new,
    )


def stream_reply(*, history, conversation, conversation_id, user_id, ephemeral,
                 model, user_row, remember, created_new=False):
    """Shared SSE generator for a normal turn, a regenerate, incognito, and a
    signed-out guest. `ephemeral` means nothing here gets written down."""
    skip = (conversation or {}).get("summarized_through") or 0
    contents = build_contents(history, skip)
    system_instruction = build_system_prompt(conversation, user_id)

    def generate():
        yield sse("start", {
            "conversation_id": conversation_id,
            "incognito": ephemeral,
            "user_message_id": user_row["id"] if user_row else None,
            "created_at": user_row["created_at"] if user_row else None,
            "title": (conversation or {}).get("title"),
            "created_new": created_new,
            "model": model,
        })

        pieces = []
        try:
            stream = client.models.generate_content_stream(
                model=model,
                contents=contents,  # the past conversation, every call
                config=types.GenerateContentConfig(system_instruction=system_instruction),
            )
            for chunk in stream:
                piece = chunk.text
                if piece:
                    pieces.append(piece)
                    yield sse("delta", {"text": piece})
        except GeneratorExit:
            # Browser hung up (stop button, or tab closed). Keep whatever we'd
            # already streamed so stored history matches what they saw.
            partial = "".join(pieces)
            if partial:
                _commit_reply(history, conversation_id, partial)
            elif user_row:
                _rollback(conversation_id, history, user_row["id"])
            raise
        except errors.APIError as e:
            if user_row:
                _rollback(conversation_id, history, user_row["id"])
            if e.code == 429:
                message = ("Rate limit hit (Gemini free tier caps requests per "
                           "minute/day). Wait a bit and try again, or check your "
                           "quota in Google AI Studio.")
            else:
                message = f"Gemini API error ({e.code}): {e.message}"
            yield sse("error", {"error": message})
            return
        except Exception as e:
            if user_row:
                _rollback(conversation_id, history, user_row["id"])
            yield sse("error", {"error": str(e)})
            return

        assistant_reply = "".join(pieces)

        # 3. Append the model's reply to the history payload too
        assistant_row = _commit_reply(history, conversation_id, assistant_reply)

        payload = {
            "conversation_id": conversation_id,
            "incognito": ephemeral,
            "message_id": assistant_row["id"] if assistant_row else None,
            "created_at": assistant_row["created_at"] if assistant_row else None,
            "history_length": len(history),
        }

        # Note: no token count here on purpose. count_tokens is a network call,
        # and doing it before `done` would keep the composer locked for an
        # extra round-trip after the answer had already finished streaming.
        # The client asks /api/context separately once it's unblocked.
        if conversation_id:
            payload["title"] = db.get_conversation(conversation_id, user_id)["title"]

        yield sse("done", payload)

        # Follow-up work: learn durable facts, and trim the transcript if it's
        # getting long. Both are best-effort and must never delay the reply, so
        # they happen strictly after `done` has been flushed to the client.
        if conversation_id:
            recent = history[-2:] if len(history) >= 2 else history
            _run_followups(user_id, conversation_id, model, recent, remember)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _run_followups(user_id, conversation_id, model, recent, remember):
    """Fact extraction and summarisation, after the reply has been delivered.

    On a normal long-running server these go on daemon threads so the request
    can return immediately. That doesn't survive serverless: the platform
    freezes or discards the instance as soon as the handler returns, and a
    detached thread is simply killed mid-flight — the bot would silently stop
    learning. There, we run them inline instead. The client already has the
    full reply rendered by this point, so the only cost is that the connection
    stays open a moment longer.
    """
    jobs = []
    if remember:
        jobs.append(lambda: learn_from_turn(user_id, conversation_id, recent))
    jobs.append(lambda: maybe_summarise(user_id, conversation_id, model))

    if SERVERLESS:
        for job in jobs:
            job()
    else:
        for job in jobs:
            threading.Thread(target=job, daemon=True).start()


def _commit_reply(history, conversation_id, text):
    """Append the assistant turn to the in-memory list, and to disk unless
    this is an incognito thread."""
    if conversation_id is None:
        history.append({"role": "model", "text": text, "id": None})
        return None
    row = db.add_message(conversation_id, "model", text)
    history.append({"role": "model", "text": text, "id": row["id"]})
    return row


def _rollback(conversation_id, history, message_id):
    """Undo the user turn we optimistically stored, so a failed call doesn't
    leave a dangling message with no reply."""
    if history and history[-1]["role"] == "user":
        history.pop()
    if conversation_id is not None and message_id is not None:
        db.delete_message(message_id)


@app.route("/api/reset", methods=["POST"])
@login_required
def reset(user_id):
    """Clear one conversation's messages (memory + disk). Without a
    conversation_id it wipes every conversation this user has."""
    data = request.get_json(force=True, silent=True) or {}
    conversation_id = data.get("conversation_id")

    if conversation_id:
        conversation_id = int(conversation_id)
        if not db.get_conversation(conversation_id, user_id):
            return jsonify({"error": "Conversation not found."}), 404
        db.clear_messages(conversation_id)
        conversation_histories[conversation_id] = []
        return jsonify({"status": "cleared", "history_length": 0})

    for conversation in db.list_conversations(user_id):
        conversation_histories.pop(conversation["id"], None)
    db.delete_all_conversations(user_id)
    return jsonify({"status": "cleared", "history_length": 0})


@app.route("/api/history", methods=["GET"])
@login_required
def get_history(user_id):
    """Expose the raw in-memory history array — handy for debugging/inspection."""
    conversation_id = request.args.get("conversation_id", type=int)
    if conversation_id:
        if not db.get_conversation(conversation_id, user_id):
            return jsonify({"error": "Conversation not found."}), 404
        return jsonify({"history": history_for(conversation_id)})

    mine = {c["id"] for c in db.list_conversations(user_id)}
    return jsonify({
        "histories": {str(k): v for k, v in conversation_histories.items()
                      if k in mine},
        "incognito_sessions": len(incognito_histories),
        "stats": db.stats(user_id),
        # Handy for confirming a deployment is talking to the right store.
        "storage": db.backend(),
        "serverless": SERVERLESS,
    })


# --- context window --------------------------------------------------------

def measure_context(history, system_instruction, model):
    """How much of the model's input window this thread currently occupies."""
    limit = context_limit(model)
    try:
        contents = build_contents(history)
        contents.append(types.Content(
            role="user", parts=[types.Part(text=system_instruction)]
        ))
        total = client.models.count_tokens(model=model, contents=contents).total_tokens
    except Exception:
        # Rough fallback so the meter still moves if the call fails.
        total = sum(len(t["text"]) for t in history) // 4
    return {
        "tokens": total,
        "limit": limit,
        "percent": round(100 * total / limit, 2) if limit else 0,
        "trim_at": SUMMARY_TRIGGER_TOKENS,
    }


@app.route("/api/context", methods=["GET"])
@login_required
def context(user_id):
    conversation_id = request.args.get("conversation_id", type=int)
    if not conversation_id:
        return jsonify({"context": {"tokens": 0, "limit": context_limit(MODEL),
                                    "percent": 0, "trim_at": SUMMARY_TRIGGER_TOKENS}})
    conversation = db.get_conversation(conversation_id, user_id)
    if not conversation:
        return jsonify({"error": "Conversation not found."}), 404
    history = history_for(conversation_id)
    model = conversation.get("model") or MODEL
    return jsonify({
        "context": measure_context(
            history, build_system_prompt(conversation, user_id), model
        )
    })


def maybe_summarise(user_id, conversation_id, model):
    """If the transcript has grown past the trim threshold, fold everything
    except the most recent turns into a prose summary and remember how far it
    reaches. Subsequent calls resend the summary instead of those turns."""
    try:
        conversation = db.get_conversation(conversation_id, user_id)
        if not conversation:
            return
        history = history_for(conversation_id)
        already = conversation.get("summarized_through") or 0

        if len(history) - already <= KEEP_RECENT_MESSAGES:
            return

        measured = measure_context(
            history, build_system_prompt(conversation, user_id), model
        )
        if measured["tokens"] < SUMMARY_TRIGGER_TOKENS:
            return

        cutoff = len(history) - KEEP_RECENT_MESSAGES
        transcript = "\n\n".join(
            f"{'User' if t['role'] == 'user' else 'Assistant'}: {t['text']}"
            for t in history[:cutoff]
        )
        previous = conversation.get("summary")
        instruction = (
            "Summarise this conversation excerpt for use as long-term context. "
            "Keep every concrete detail the assistant would need later: names, "
            "preferences, decisions, unresolved questions, code or data "
            "discussed. Write compact prose, no preamble."
        )
        if previous:
            instruction += (
                "\n\nFold in this existing summary of even earlier turns:\n" + previous
            )

        response = client.models.generate_content(
            model=UTILITY_MODEL,
            contents=transcript,
            config=types.GenerateContentConfig(system_instruction=instruction),
        )
        summary = (response.text or "").strip()
        if summary:
            db.update_conversation(
                conversation_id, user_id, summary=summary, summarized_through=cutoff
            )
    except Exception as e:
        print(f"[summarise] skipped: {e}")


# --- long-term memory ------------------------------------------------------

MEMORY_INSTRUCTION = (
    "Extract durable facts that the USER stated about themselves — things "
    "worth remembering in a completely different conversation weeks from now.\n\n"
    "Include: their name, job, location, stack and tools, stated preferences, "
    "ongoing projects, constraints they mention.\n"
    "Exclude: anything the assistant said or inferred, one-off questions, "
    "transient task details, and anything phrased as a question.\n\n"
    "Write each fact as a short third-person sentence starting with 'The user'. "
    "Return an empty list if the exchange contains nothing new and durable — "
    "that is by far the common case, so prefer it over inventing something."
)

# Two facts counted as "the same" above this word-overlap ratio. Catches the
# paraphrases an LLM produces run to run ("prefers dark mode" vs "strongly
# prefers dark mode UIs") that a UNIQUE column never would.
DUPLICATE_SIMILARITY = 0.6
_STOPWORDS = {"the", "user", "a", "an", "is", "are", "of", "and", "to", "in",
              "with", "for", "on", "their", "they", "them", "has", "have"}


def _fingerprint(text):
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _is_near_duplicate(fact, existing_fingerprints):
    """Jaccard overlap against everything already known."""
    incoming = _fingerprint(fact)
    if not incoming:
        return True
    for known in existing_fingerprints:
        union = incoming | known
        if union and len(incoming & known) / len(union) >= DUPLICATE_SIMILARITY:
            return True
    return False


def learn_from_turn(user_id, conversation_id, turns):
    """Background: pull any durable facts out of the latest exchange and store
    them. Best-effort — a failure here must never affect the chat itself.

    Two layers of de-duplication, because without them the same fact gets
    re-learned in slightly different words on every single turn:
      1. the model is shown what's already known and told not to restate it
      2. anything that slips through is caught by word-overlap below
    """
    try:
        # Only the user's own words. Including the assistant's reply meant that
        # whenever it summarised back what it knew ("You're Hassan, a student
        # in Lahore…"), every one of those facts got re-extracted as new.
        said = "\n\n".join(t["text"] for t in turns if t["role"] == "user")
        if not said.strip():
            return

        known = db.list_memories(user_id)
        instruction = MEMORY_INSTRUCTION
        if known:
            instruction += (
                "\n\nAlready known — do NOT return these again, and do not "
                "return a reworded version of any of them:\n"
                + "\n".join(f"- {m['text']}" for m in known[:80])
            )

        response = client.models.generate_content(
            model=UTILITY_MODEL,
            contents=said,
            config=types.GenerateContentConfig(
                system_instruction=instruction,
                response_mime_type="application/json",
                response_schema=list[str],
            ),
        )
        facts = json.loads(response.text or "[]")
        if not isinstance(facts, list):
            return

        fingerprints = [_fingerprint(m["text"]) for m in known]
        for fact in facts[:5]:
            fact = str(fact).strip()
            if not (8 < len(fact) <= 300):
                continue
            if _is_near_duplicate(fact, fingerprints):
                continue
            db.add_memory(user_id, fact, conversation_id)
            fingerprints.append(_fingerprint(fact))
    except Exception as e:
        print(f"[memory] skipped: {e}")


CONSOLIDATE_INSTRUCTION = (
    "You are given a list of remembered facts about one user. Rewrite it as "
    "the SHORTEST list that still contains every piece of information.\n\n"
    "- Merge entries that mean the same thing, even when the wording differs "
    "completely ('lives in Lahore' and 'is located in Lahore' are one fact).\n"
    "- Merge entries where one fully contains the other, keeping the more "
    "specific version.\n"
    "- Combine closely related facts into one sentence where it reads "
    "naturally.\n"
    "- Never invent anything, and never lose a detail — every name, place, "
    "tool and preference in the input must survive somewhere in the output.\n\n"
    "Keep the 'The user …' third-person phrasing. Return a JSON array of strings."
)


def _jaccard_dedupe(user_id):
    """Cheap fallback: collapse facts by word overlap, keeping the longest
    (usually most specific) wording of each cluster."""
    memories = sorted(db.list_memories(user_id), key=lambda m: -len(m["text"]))
    kept, removed = [], 0
    for memory in memories:
        if _is_near_duplicate(memory["text"], kept):
            db.delete_memory(memory["id"], user_id)
            removed += 1
        else:
            kept.append(_fingerprint(memory["text"]))
    return removed


@app.route("/api/memories/dedupe", methods=["POST"])
@login_required
def dedupe_memories(user_id):
    """Consolidate the memory list.

    Word overlap alone can't tell that "is Hassan" and "user's name is Hassan"
    are the same fact, so the real work is done by the model. The overlap pass
    is kept as a fallback for when that call fails.
    """
    before = db.list_memories(user_id)
    if len(before) < 2:
        return jsonify({"removed": 0, "memories": before})

    try:
        response = client.models.generate_content(
            model=UTILITY_MODEL,
            contents="\n".join(f"- {m['text']}" for m in before),
            config=types.GenerateContentConfig(
                system_instruction=CONSOLIDATE_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=list[str],
            ),
        )
        merged = json.loads(response.text or "[]")
        merged = [str(f).strip() for f in merged if str(f).strip()]

        # Sanity gate: a consolidation that empties the list, or somehow grows
        # it, is a bad response — keep what we had rather than trusting it.
        if not merged or len(merged) > len(before):
            raise ValueError("implausible consolidation")

        db.clear_memories(user_id)
        for fact in merged:
            db.add_memory(user_id, fact[:300])
        removed = len(before) - len(db.list_memories(user_id))
    except Exception as e:
        print(f"[memory] consolidation fell back to word overlap: {e}")
        removed = _jaccard_dedupe(user_id)

    return jsonify({"removed": removed, "memories": db.list_memories(user_id)})


@app.route("/api/memories", methods=["GET"])
@login_required
def get_memories(user_id):
    return jsonify({"memories": db.list_memories(user_id)})


@app.route("/api/memories", methods=["POST"])
@login_required
def create_memory(user_id):
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "A memory needs some text."}), 400
    db.add_memory(user_id, text[:300])
    return jsonify({"memories": db.list_memories(user_id)}), 201


@app.route("/api/memories/<int:memory_id>", methods=["DELETE"])
@login_required
def remove_memory(user_id, memory_id):
    db.delete_memory(memory_id, user_id)
    return jsonify({"status": "deleted", "id": memory_id})


@app.route("/api/memories", methods=["DELETE"])
@login_required
def remove_all_memories(user_id):
    db.clear_memories(user_id)
    return jsonify({"status": "cleared"})


# --- search ----------------------------------------------------------------

@app.route("/api/search", methods=["GET"])
@login_required
def search(user_id):
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"query": "", "results": []})
    return jsonify({"query": query, "results": db.search_messages(user_id, query)})


# --- text to speech --------------------------------------------------------

@app.route("/api/voices", methods=["GET"])
def voices():
    return jsonify({
        "voices": tts.VOICES,
        "default": tts.DEFAULT_VOICE,
        "available": tts.AVAILABLE,
    })


@app.route("/api/tts", methods=["POST"])
def speak():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Nothing to speak."}), 400

    try:
        audio = tts.synthesize(
            text,
            voice=data.get("voice") or tts.DEFAULT_VOICE,
            rate=data.get("rate", 0),
            pitch=data.get("pitch", 0),
        )
    except RuntimeError as e:
        # 503 tells the frontend to fall back to the browser's own voices.
        return jsonify({"error": str(e)}), 503

    return Response(audio, mimetype="audio/mpeg", headers={
        "Cache-Control": "no-store",
        "Content-Length": str(len(audio)),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
