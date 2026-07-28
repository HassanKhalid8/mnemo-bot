"""Vercel serverless entry point.

Vercel's Python runtime looks for a WSGI callable named `app` inside `api/`.
The real application lives at the project root next to its templates/ and
static/ directories, so this just puts the root on the import path and
re-exports it — Flask then resolves those directories relative to app.py as
it normally would.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402  (import must follow the sys.path tweak)

__all__ = ["app"]
