"""
Text-to-speech using Microsoft Edge's neural voices (via `edge-tts`).
---------------------------------------------------------------------
Why this one: it needs no API key, no account, and no per-month character
quota — `edge-tts` speaks to the same free endpoint Edge's "Read aloud"
feature uses, so there is no token budget to burn through. The voices are
the same neural models Azure Speech sells, so they sound genuinely AI, not
like the robotic built-in system voices.

There is still a fair-use rate limit on Microsoft's side (hammer it with
hundreds of requests a second and it will start refusing), but for normal
chat playback it is effectively unlimited.

Fallback: if this module can't reach the service, `app.py` returns a 503 and
the frontend quietly falls back to the browser's built-in speechSynthesis.
"""

import asyncio
import re

try:
    import edge_tts
    AVAILABLE = True
except ImportError:  # pragma: no cover - only hit when the dep isn't installed
    edge_tts = None
    AVAILABLE = False


DEFAULT_VOICE = "en-US-AvaNeural"

# A curated shortlist — `edge-tts --list-voices` has ~450 more if you want to
# add others. Each entry is what the voice picker in the UI renders.
VOICES = [
    {"id": "en-US-AvaNeural",          "name": "Ava",      "accent": "US",        "gender": "Female", "vibe": "Warm & expressive"},
    {"id": "en-US-AndrewNeural",       "name": "Andrew",   "accent": "US",        "gender": "Male",   "vibe": "Warm & confident"},
    {"id": "en-US-EmmaNeural",         "name": "Emma",     "accent": "US",        "gender": "Female", "vibe": "Cheerful & clear"},
    {"id": "en-US-BrianNeural",        "name": "Brian",    "accent": "US",        "gender": "Male",   "vibe": "Casual & sincere"},
    {"id": "en-US-AriaNeural",         "name": "Aria",     "accent": "US",        "gender": "Female", "vibe": "Confident newsreader"},
    {"id": "en-US-ChristopherNeural",  "name": "Chris",    "accent": "US",        "gender": "Male",   "vibe": "Deep & authoritative"},
    {"id": "en-GB-SoniaNeural",        "name": "Sonia",    "accent": "British",   "gender": "Female", "vibe": "Crisp & friendly"},
    {"id": "en-GB-RyanNeural",         "name": "Ryan",     "accent": "British",   "gender": "Male",   "vibe": "Smooth & relaxed"},
    {"id": "en-AU-NatashaNeural",      "name": "Natasha",  "accent": "Australian", "gender": "Female", "vibe": "Bright & upbeat"},
    {"id": "en-IN-NeerjaNeural",       "name": "Neerja",   "accent": "Indian",    "gender": "Female", "vibe": "Clear & measured"},
    {"id": "en-IN-PrabhatNeural",      "name": "Prabhat",  "accent": "Indian",    "gender": "Male",   "vibe": "Calm & steady"},
    {"id": "ur-PK-UzmaNeural",         "name": "Uzma",     "accent": "Urdu",      "gender": "Female", "vibe": "Native Urdu"},
    {"id": "ur-PK-AsadNeural",         "name": "Asad",     "accent": "Urdu",      "gender": "Male",   "vibe": "Native Urdu"},
    {"id": "hi-IN-SwaraNeural",        "name": "Swara",    "accent": "Hindi",     "gender": "Female", "vibe": "Native Hindi"},
]

VALID_VOICE_IDS = {v["id"] for v in VOICES}

# Hard ceiling per request so one enormous reply can't hang the server.
MAX_CHARS = 6000


def strip_markdown(text):
    """Speak the prose, not the syntax. Code fences get skipped entirely —
    reading out a Python block character by character is nobody's idea of a
    good time."""
    text = re.sub(r"```[\s\S]*?```", " (code block) ", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)          # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)        # links -> label
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)   # headings
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.M)        # blockquotes
    text = re.sub(r"^\s*[-*_]{3,}\s*$", " ", text, flags=re.M)  # rules
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)        # bullets
    text = re.sub(r"(\*\*|__|\*|_|~~)", "", text)               # emphasis
    text = re.sub(r"\|", " ", text)                             # table pipes
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clamp_percent(value, lo=-50, hi=100):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    n = max(lo, min(hi, n))
    return f"{n:+d}%"


async def _synthesize_async(text, voice, rate, pitch):
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    chunks = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    return b"".join(chunks)


def synthesize(text, voice=DEFAULT_VOICE, rate=0, pitch=0):
    """Return MP3 bytes for `text`. Raises RuntimeError on failure."""
    if not AVAILABLE:
        raise RuntimeError(
            "edge-tts isn't installed. Run: pip install -r requirements.txt"
        )

    spoken = strip_markdown(text)[:MAX_CHARS]
    if not spoken:
        raise RuntimeError("Nothing to read out — the message has no spoken text.")

    if voice not in VALID_VOICE_IDS:
        voice = DEFAULT_VOICE

    rate_str = _clamp_percent(rate)
    pitch_val = max(-50, min(50, int(pitch) if str(pitch).lstrip("+-").isdigit() else 0))
    pitch_str = f"{pitch_val:+d}Hz"

    try:
        audio = asyncio.run(_synthesize_async(spoken, voice, rate_str, pitch_str))
    except Exception as e:
        raise RuntimeError(f"Voice service unreachable: {e}") from e

    if not audio:
        raise RuntimeError("Voice service returned no audio.")
    return audio
