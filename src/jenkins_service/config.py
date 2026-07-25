from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from pydantic import BeforeValidator, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode, BeforeValidator(_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    service_version: str = "0.1.0"
    public_base_url: HttpUrl | None = None
    database_url: str = "postgresql://jenkinsservice@postgres:5432/jenkinsservice"
    database_password_file: Path | None = Path("/run/secrets/postgres_password")
    jenkins_url: str = "http://jenkins:8080"
    jenkins_user: str = "jenkinsservice"
    jenkins_token_file: Path = Path("/run/secrets/jenkins_api_token")
    api_tokens_file: Path = Path("/run/secrets/api_tokens")
    github_read_token_file: Path = Path("/run/secrets/github_read_pat")
    github_write_token_file: Path = Path("/run/secrets/github_write_pat")
    github_webhook_secret_file: Path = Path("/run/secrets/github_webhook_secret")
    github_api_url: str = "https://api.github.com"
    github_web_url: str = "https://github.com"
    github_allowlist: CsvList = Field(default_factory=list)
    extension_allowlist: CsvList = Field(default_factory=list)
    extension_runner_url: str = "http://extension-runner:8090"
    allowed_origins: CsvList = Field(default_factory=list)
    trust_proxy_headers: bool = True
    require_https: bool = True
    rate_limit_per_minute: int = Field(default=120, ge=1, le=10_000)
    max_webhook_bytes: int = Field(default=2_000_000, ge=1)
    max_artifact_bytes: int = Field(default=100_000_000, ge=1)
    max_extension_output_bytes: int = Field(default=1_000_000, ge=1)
    log_redaction_patterns: CsvList = Field(
        default_factory=lambda: [
            r"(?i)(authorization:\s*bearer\s+)\S+",
            r"(?i)((?:token|password|secret)\s*[=:]\s*)\S+",
        ]
    )

    @field_validator("public_base_url")
    @classmethod
    def public_url_is_https(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("PUBLIC_BASE_URL must use HTTPS")
        return value

    def read_secret(self, path: Path) -> str:
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"secret file is empty: {path}")
        return value

    def postgres_dsn(self) -> str:
        if not self.database_password_file or not self.database_password_file.exists():
            return self.database_url
        password = self.read_secret(self.database_password_file)
        separator = "&" if "?" in self.database_url else "?"
        return f"{self.database_url}{separator}password={quote_plus(password)}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
