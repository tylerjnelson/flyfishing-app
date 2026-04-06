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
        self.uploads_path = os.environ.get("UPLOADS_PATH", "/data/uploads")
        self.resend_api_key = _require("RESEND_API_KEY")
        self.mail_from = _require("MAIL_FROM")
        self.airnow_api_key = _require("AIRNOW_API_KEY")
        self.here_api_key = _require("HERE_API_KEY")
        # NPS Developer API key — free at https://www.nps.gov/subjects/developer/get-started.htm
        # Falls back to DEMO_KEY (50 req/hr) if not set; real key allows 1000 req/hr.
        self.nps_api_key = os.environ.get("NPS_API_KEY", "DEMO_KEY")


settings = Settings()
