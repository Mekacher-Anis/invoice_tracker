from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class IMAPOAuthConfig:
    access_token_env: str | None = "IMAP_ACCESS_TOKEN"
    refresh_token_env: str | None = None
    client_id_env: str | None = None
    client_secret_env: str | None = None
    token_url: str | None = None
    scope: str | None = None


@dataclass(frozen=True)
class IMAPConfig:
    host: str
    username: str
    password_env: str | None = "IMAP_PASSWORD"
    auth_method: str = "password"
    oauth: IMAPOAuthConfig | None = None
    mailbox: str = "INBOX"
    search_criteria: str = "ALL"
    port: int = 993
    use_ssl: bool = True
    max_messages: int | None = None


@dataclass(frozen=True)
class OpenAIConfig:
    enabled: bool = True
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str | None = None
    model: str = "gpt-4.1-mini"
    temperature: float = 0.0


@dataclass(frozen=True)
class StorageConfig:
    database_path: Path
    attachments_dir: Path
    downloads_dir: Path
    raw_email_dir: Path


@dataclass(frozen=True)
class ProcessingConfig:
    body_char_limit: int = 12000
    pdf_char_limit: int = 20000
    pdf_image_dpi: int = 150
    max_pdf_pages_for_llm: int = 8
    allow_link_download: bool = True
    download_timeout_seconds: int = 20
    user_agent: str = "invoice-tracker/1.0"
    parallel_workers: int = 4
    max_in_flight: int = 8
    resume_enabled: bool = True
    resume_state_path: Path = Path("data/resume_state.json")
    dashboard_keywords: list[str] = field(
        default_factory=lambda: ["dashboard", "portal", "account", "billing"]
    )


@dataclass(frozen=True)
class AppConfig:
    imap: IMAPConfig
    openai: OpenAIConfig
    storage: StorageConfig
    processing: ProcessingConfig

    def require_env(self, name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise ValueError(f"Required environment variable '{name}' is not set.")
        return value

    def optional_env(self, name: str) -> str | None:
        value = os.getenv(name, "").strip()
        return value or None

    def get_imap_password(self) -> str:
        return self.require_env(self.imap.password_env)

    def get_openai_api_key(self) -> str | None:
        if not self.openai.enabled:
            return None
        return self.optional_env(self.openai.api_key_env)


def _expect_mapping(value: Any, section_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Section '{section_name}' must be a mapping.")
    return value


def _require_str(mapping: dict[str, Any], key: str, section_name: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{section_name}.{key}' must be a non-empty string.")
    return value.strip()


def _optional_str(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_openai_base_url(value: Any) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip().rstrip("/")
    if not normalized:
        return None

    lower_normalized = normalized.lower()
    for suffix in ("/chat/completions", "/completions", "/responses"):
        if lower_normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].rstrip("/")
            break

    return normalized or None


def _resolve_path(base_dir: Path, path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    root = _expect_mapping(raw, "root")
    base_dir = config_path.parent.resolve()

    imap_raw = _expect_mapping(root.get("imap", {}), "imap")
    openai_raw = _expect_mapping(root.get("openai", {}), "openai")
    storage_raw = _expect_mapping(root.get("storage", {}), "storage")
    processing_raw = _expect_mapping(root.get("processing", {}), "processing")

    auth_method = str(imap_raw.get("auth_method", "password")).strip().lower()
    if auth_method == "xoauth2":
        auth_method = "oauth2"
    if auth_method not in {"password", "oauth2"}:
        raise ValueError("imap.auth_method must be 'password' or 'oauth2'.")

    password_env = _optional_str(imap_raw, "password_env")
    if password_env is None and auth_method == "password":
        password_env = "IMAP_PASSWORD"

    oauth_raw_value = imap_raw.get("oauth")
    oauth_raw = _expect_mapping(oauth_raw_value, "imap.oauth") if oauth_raw_value else {}
    oauth_cfg = IMAPOAuthConfig(
        access_token_env=_optional_str(oauth_raw, "access_token_env")
        or "IMAP_ACCESS_TOKEN",
        refresh_token_env=_optional_str(oauth_raw, "refresh_token_env"),
        client_id_env=_optional_str(oauth_raw, "client_id_env"),
        client_secret_env=_optional_str(oauth_raw, "client_secret_env"),
        token_url=_optional_str(oauth_raw, "token_url"),
        scope=_optional_str(oauth_raw, "scope"),
    )

    if auth_method == "password" and not password_env:
        raise ValueError("imap.password_env is required when imap.auth_method=password.")

    if auth_method == "oauth2":
        has_access_token_env = bool(oauth_cfg.access_token_env)
        has_refresh_flow = bool(
            oauth_cfg.refresh_token_env
            and oauth_cfg.client_id_env
            and oauth_cfg.client_secret_env
            and oauth_cfg.token_url
        )
        if not has_access_token_env and not has_refresh_flow:
            raise ValueError(
                "imap.oauth must provide access_token_env or "
                "refresh_token_env+client_id_env+client_secret_env+token_url "
                "when imap.auth_method=oauth2."
            )

    imap_cfg = IMAPConfig(
        host=_require_str(imap_raw, "host", "imap"),
        username=_require_str(imap_raw, "username", "imap"),
        password_env=password_env,
        auth_method=auth_method,
        oauth=oauth_cfg,
        mailbox=str(imap_raw.get("mailbox", "INBOX")),
        search_criteria=str(imap_raw.get("search_criteria", "ALL")),
        port=int(imap_raw.get("port", 993)),
        use_ssl=bool(imap_raw.get("use_ssl", True)),
        max_messages=(
            int(imap_raw["max_messages"])
            if imap_raw.get("max_messages") is not None
            else None
        ),
    )

    openai_cfg = OpenAIConfig(
        enabled=bool(openai_raw.get("enabled", True)),
        api_key_env=str(openai_raw.get("api_key_env", "OPENAI_API_KEY")),
        base_url=_normalize_openai_base_url(openai_raw.get("base_url")),
        model=str(openai_raw.get("model", "gpt-4.1-mini")),
        temperature=float(openai_raw.get("temperature", 0.0)),
    )

    storage_cfg = StorageConfig(
        database_path=_resolve_path(
            base_dir,
            _require_str(storage_raw, "database_path", "storage"),
        ),
        attachments_dir=_resolve_path(
            base_dir,
            _require_str(storage_raw, "attachments_dir", "storage"),
        ),
        downloads_dir=_resolve_path(
            base_dir,
            _require_str(storage_raw, "downloads_dir", "storage"),
        ),
        raw_email_dir=_resolve_path(
            base_dir,
            _require_str(storage_raw, "raw_email_dir", "storage"),
        ),
    )

    dashboard_keywords = processing_raw.get("dashboard_keywords")
    if dashboard_keywords is None:
        dashboard_keywords = ["dashboard", "portal", "account", "billing"]
    if not isinstance(dashboard_keywords, list):
        raise ValueError("'processing.dashboard_keywords' must be a list of strings.")

    processing_cfg = ProcessingConfig(
        body_char_limit=int(processing_raw.get("body_char_limit", 12000)),
        pdf_char_limit=int(processing_raw.get("pdf_char_limit", 20000)),
        pdf_image_dpi=max(72, int(processing_raw.get("pdf_image_dpi", 150))),
        max_pdf_pages_for_llm=max(
            1, int(processing_raw.get("max_pdf_pages_for_llm", 8))
        ),
        allow_link_download=bool(processing_raw.get("allow_link_download", True)),
        download_timeout_seconds=int(
            processing_raw.get("download_timeout_seconds", 20)
        ),
        user_agent=str(processing_raw.get("user_agent", "invoice-tracker/1.0")),
        parallel_workers=max(1, int(processing_raw.get("parallel_workers", 4))),
        max_in_flight=max(1, int(processing_raw.get("max_in_flight", 8))),
        resume_enabled=bool(processing_raw.get("resume_enabled", True)),
        resume_state_path=_resolve_path(
            base_dir,
            str(processing_raw.get("resume_state_path", "data/resume_state.json")),
        ),
        dashboard_keywords=[str(x).lower() for x in dashboard_keywords if str(x).strip()],
    )

    return AppConfig(
        imap=imap_cfg,
        openai=openai_cfg,
        storage=storage_cfg,
        processing=processing_cfg,
    )
