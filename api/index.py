"""Vercel serverless entry point for the RailSaathi AI backend.

Vercel's Python runtime exposes ASGI applications exported as `app`. This
file imports the FastAPI application from the backend package and re-exports
it so every backend route (/api/v1/health, /api/v1/agent/chat,
/api/v1/journeys/search, ...) is served by the serverless function.

Routing on Vercel (see ../vercel.json):
- static `index.html` at the project root is served at `/` by the platform;
- every other path is rewritten to this function with the original request
  path preserved, so FastAPI's own routing handles it.

Configuration comes from Vercel project environment variables (never
committed): RAILCORE_API_KEY / RAILCORE_API_KEYS, GEMINI_API_KEY,
LLM_PROVIDER, RAILCORE_BASE_URL, ... (see .env.example for the full list).
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.app.main import app  # noqa: E402,F401
