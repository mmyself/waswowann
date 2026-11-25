from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent


def _load_env() -> None:
    # Load only the backend's explicit .env; never depend on the process cwd.
    path = BACKEND_DIR / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _configured_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default)).expanduser()
    return (BACKEND_DIR / path).resolve() if not path.is_absolute() else path.resolve()


def _secret_value(variable: str, file_variable: str, default_file: str = "") -> str:
    value = os.getenv(variable, "").strip()
    if value:
        return value
    file_name = os.getenv(file_variable, default_file).strip()
    if not file_name:
        return ""
    path = Path(file_name).expanduser()
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    if path.name == ".env":
        for line in content.splitlines():
            key, separator, secret = line.partition("=")
            if separator and key.strip() == variable:
                return secret.strip().strip("\"'")
        return ""
    return content


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    frontend_origin: str
    database_path: Path
    photo_storage_dir: Path
    temp_storage_dir: Path
    local_import_dir: Path
    import_timezone: str
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    gemini_provider: str = "google"
    gemini_model: str = "gemini-2.5-flash"
    google_api_key: str = ""
    antigravity_bridge_url: str = ""
    antigravity_bridge_auth: str = ""
    fusion_default_sigma_meters: float = 4.0
    fusion_default_sigma_seconds: float = 60.0
    frontend_allowed_origins: tuple[str, ...] = ()
    metacore_db_path: Path | None = None

    @property
    def resolved_metacore_db_path(self) -> Path:
        return self.metacore_db_path if self.metacore_db_path is not None else self.database_path.with_name('metacore.sqlite3')

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        origins = [self.frontend_origin, *self.frontend_allowed_origins]
        return tuple(dict.fromkeys(o for o in origins if o))

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def analysis_enabled(self) -> bool:
        if self.gemini_provider == "antigravity":
            return bool(self.antigravity_bridge_url and self.antigravity_bridge_auth)
        return bool(self.google_api_key)

    @classmethod
    def load(cls) -> "Settings":
        _load_env()
        return cls(
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8000")),
            frontend_origin=os.getenv("FRONTEND_ORIGIN", "http://localhost:3000").rstrip("/"),
            frontend_allowed_origins=tuple(
                item.strip().rstrip("/")
                for item in os.getenv("FRONTEND_ALLOWED_ORIGINS", "").split(",")
                if item.strip()
            ),
            database_path=_configured_path("DATABASE_PATH", "../data/waswowann.sqlite3"),
            photo_storage_dir=_configured_path("PHOTO_STORAGE_DIR", "../data/photos"),
            temp_storage_dir=_configured_path("TEMP_STORAGE_DIR", "../data/tmp"),
            local_import_dir=_configured_path("LOCAL_IMPORT_DIR", "../data/incoming"),
            import_timezone=os.getenv("IMPORT_TIMEZONE", "Europe/Berlin"),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            google_redirect_uri=os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/google/oauth/callback"),
            gemini_provider=os.getenv("GEMINI_PROVIDER", "google").lower(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            google_api_key=_secret_value("GOOGLE_API_KEY", "GOOGLE_API_KEY_FILE", "~/.hermes/.env"),
            antigravity_bridge_url=os.getenv("AGY_BRIDGE_URL", "http://127.0.0.1:18095/v1").rstrip("/"),
            antigravity_bridge_auth=_secret_value("AGY_BRIDGE_AUTH_KEY", "AGY_BRIDGE_AUTH_KEY_FILE", "~/.hermes/secrets/agy_bridge_key"),
            fusion_default_sigma_meters=float(os.getenv("FUSION_DEFAULT_SIGMA_METERS", "4")),
            fusion_default_sigma_seconds=float(os.getenv("FUSION_DEFAULT_SIGMA_SECONDS", "60")),
            metacore_db_path=_configured_path("METACORE_DB_PATH", "../data/metacore.sqlite3"),
        )


settings = Settings.load()
