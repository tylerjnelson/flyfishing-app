import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable '{name}' is not set")
    return value


class Settings:
    def __init__(self) -> None:
        self.app_secret_key = _require("APP_SECRET_KEY")
        self.database_url = _require("DATABASE_URL")
        self.ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        # Chat-engine switchover (Phase 1, Option B). "ollama" (default) keeps the
        # gemma chat + utility traffic on ollama; "llamacpp" routes it to the two
        # dedicated llama-server instances below. Embeddings (nomic-embed) and vision
        # (llama3.2-vision) always stay on ollama regardless. The flag flips the chat
        # path AND the three background utility sites together, so rollback is atomic
        # (never split-brain) — see agentic-harness-plan.md Phase 1. Enable with
        # FLYFISH_CHAT_ENGINE=llamacpp.
        self.chat_engine = os.environ.get("FLYFISH_CHAT_ENGINE", "ollama").lower()
        # Interactive chat instance (--reasoning on, tools). Utility instance
        # (--reasoning off) serves field/location extraction + debrief. Both are
        # localhost-only llama-server units; see deploy/llama-*.service.
        self.llama_chat_url = os.environ.get("FLYFISH_LLAMA_CHAT_URL", "http://127.0.0.1:8080")
        self.llama_util_url = os.environ.get("FLYFISH_LLAMA_UTIL_URL", "http://127.0.0.1:8081")
        # Agentic-harness rollout (Phase 3/8). "twopass" (default) keeps the existing
        # planning-then-generation path; "agentic" runs the single tool-calling loop
        # (chat/router.py). ORTHOGONAL to FLYFISH_CHAT_ENGINE — agentic ships on the
        # llama.cpp engine, but the flag is independent so either flips back alone (the
        # loop also works on ollama's /api/chat tool streaming). Enable with
        # FLYFISH_HARNESS_MODE=agentic. See agentic-harness-plan.md Phase 3.
        self.harness_mode = os.environ.get("FLYFISH_HARNESS_MODE", "twopass").lower()
        self.uploads_path = os.environ.get("UPLOADS_PATH", "/data/uploads")
        self.resend_api_key = _require("RESEND_API_KEY")
        self.mail_from = _require("MAIL_FROM")
        self.airnow_api_key = _require("AIRNOW_API_KEY")
        self.here_api_key = _require("HERE_API_KEY")
        # Kill-switch for ALL HERE traffic (routing + geocoding). When set, drive
        # times come from deterministic Haversine and geocoding is skipped — used
        # by batch/benchmark runs so they never touch the HERE API (spec §11.1).
        # Off in production. Enable with FLYFISH_DISABLE_HERE=1.
        self.here_disabled = os.environ.get("FLYFISH_DISABLE_HERE", "").lower() in ("1", "true", "yes")
        # NPS Developer API key — free at https://www.nps.gov/subjects/developer/get-started.htm
        # Falls back to DEMO_KEY (50 req/hr) if not set; real key allows 1000 req/hr.
        self.nps_api_key = os.environ.get("NPS_API_KEY", "DEMO_KEY")


settings = Settings()
