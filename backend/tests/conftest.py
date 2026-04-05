"""
Shared pytest fixtures for Phase 1 tests.

The Settings class validates env vars at import time, so we must set
required env vars before any backend module is imported.  This conftest
runs first and patches os.environ before the test session begins.
"""

import os
import sys

# Ensure backend/ is on the path (pytest runs from backend/)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# Ensure repo root is on the path so prompts/ is importable (mirrors main.py sys.path setup)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# Minimal env vars required by config.Settings.__init__()
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://flyfish_app:test@localhost/flyfish_test")
os.environ.setdefault("RESEND_API_KEY", "re_test")
os.environ.setdefault("MAIL_FROM", "test@example.com")
os.environ.setdefault("AIRNOW_API_KEY", "test-airnow-key")
os.environ.setdefault("HERE_API_KEY", "test-here-key")
