from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# The value `development_api_key` ships with. Fine on a laptop on a LAN, and the one string
# that must never be the credential guarding a real participant dataset.
SHIPPED_DEFAULT_API_KEY = "change-me-development-key"


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://quit_smoke_app:change-me@localhost:5432/quit_smoke_research"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    environment: str = "development"
    log_level: str = "INFO"
    auth_enabled: bool = True
    development_api_key: str = SHIPPED_DEFAULT_API_KEY
    # Where llama-server is listening, on this same machine. Empty disables the generate
    # route entirely, which is a supported state: the app treats an unreachable model as
    # fallback-only, so a backend with no inference attached still works.
    llama_server_url: str = ""
    llama_timeout_seconds: float = 120.0

    export_directory: str = "exports"
    backup_directory: str = "backups"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def verify_deployment_is_safe(settings: Settings) -> None:
    """Refuses to start with the development shortcuts on outside development.

    Both of these are survivable on a laptop on a LAN and neither is survivable anywhere else,
    and — the reason this exists rather than a line in the README — **neither announces itself
    at runtime.** The server starts, `/health` says ok, the dashboard works, and the whole
    study dataset is served to anyone who asks.

    `auth_enabled=False` is the worse of the two: `require_api_key` returns early on it, so it
    switches off the device key *and* the operator session check together.
    """
    if settings.environment.strip().lower() == "development":
        return

    problems = []
    if not settings.auth_enabled:
        problems.append(
            "AUTH_ENABLED is false, which disables the device API key and the operator session check"
        )
    if settings.development_api_key == SHIPPED_DEFAULT_API_KEY:
        problems.append("DEVELOPMENT_API_KEY is still the shipped default")

    if problems:
        raise RuntimeError(
            f"Refusing to start with ENVIRONMENT={settings.environment!r}: " + "; ".join(problems)
        )