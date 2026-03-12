from __future__ import annotations

import json
import secrets
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from .config import AppConfig

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class GoogleOAuthTokens:
    access_token: str
    refresh_token: str | None
    token_type: str | None
    scope: str | None
    expires_in: int | None


def _post_form(url: str, payload: dict[str, str], timeout_seconds: int = 30) -> dict:
    encoded = urlencode(payload).encode("utf-8")
    request = Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _request_access_token_from_refresh_flow(config: AppConfig) -> str | None:
    oauth_cfg = config.imap.oauth
    if oauth_cfg is None:
        return None
    if not (
        oauth_cfg.refresh_token_env
        and oauth_cfg.client_id_env
        and oauth_cfg.client_secret_env
        and oauth_cfg.token_url
    ):
        return None

    refresh_token = config.require_env(oauth_cfg.refresh_token_env)
    client_id = config.require_env(oauth_cfg.client_id_env)
    client_secret = config.require_env(oauth_cfg.client_secret_env)

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if oauth_cfg.scope:
        payload["scope"] = oauth_cfg.scope

    data = _post_form(oauth_cfg.token_url, payload, timeout_seconds=30)
    token = data.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("OAuth token endpoint did not return 'access_token'.")
    return token.strip()


def resolve_imap_oauth_token(config: AppConfig) -> str:
    oauth_cfg = config.imap.oauth
    if oauth_cfg is None:
        raise ValueError("imap.oauth configuration is missing.")

    if oauth_cfg.access_token_env:
        token = config.optional_env(oauth_cfg.access_token_env)
        if token:
            return token

    refreshed = _request_access_token_from_refresh_flow(config)
    if refreshed:
        return refreshed

    if oauth_cfg.access_token_env:
        raise ValueError(
            f"OAuth access token not found in env '{oauth_cfg.access_token_env}'."
        )
    raise ValueError(
        "Unable to resolve OAuth access token. Configure either "
        "imap.oauth.access_token_env or refresh-token flow settings."
    )


def _wait_for_google_callback_code(
    *,
    redirect_host: str,
    redirect_port: int,
    callback_path: str,
    expected_state: str,
    timeout_seconds: int,
) -> str:
    result: dict[str, str | None] = {"code": None, "error": None, "state": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            query = parse_qs(parsed.query)
            result["code"] = (query.get("code") or [None])[0]
            result["error"] = (query.get("error") or [None])[0]
            result["state"] = (query.get("state") or [None])[0]

            success = result["code"] is not None and result["error"] is None
            self.send_response(200 if success else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if success:
                self.wfile.write(
                    b"<html><body><h3>Access granted. You can close this tab.</h3></body></html>"
                )
            else:
                self.wfile.write(
                    b"<html><body><h3>Authorization failed. Return to terminal.</h3></body></html>"
                )

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    try:
        server = HTTPServer((redirect_host, redirect_port), CallbackHandler)
    except OSError as exc:
        raise RuntimeError(
            f"Could not bind callback server on {redirect_host}:{redirect_port}. "
            "Pick a different --redirect-port."
        ) from exc

    server.timeout = 1.0
    deadline = time.time() + timeout_seconds

    try:
        while time.time() < deadline:
            if result["code"] or result["error"]:
                break
            server.handle_request()
    finally:
        server.server_close()

    if not result["code"] and not result["error"]:
        raise TimeoutError(
            "OAuth callback timed out. Re-run with a larger --oauth-timeout-seconds."
        )
    if result["error"]:
        raise RuntimeError(f"OAuth authorization failed: {result['error']}")
    if result["state"] != expected_state:
        raise RuntimeError("OAuth state mismatch. Please retry.")
    if not result["code"]:
        raise RuntimeError("OAuth callback did not include an authorization code.")
    return result["code"]


def bootstrap_google_oauth_tokens(
    *,
    client_id: str,
    client_secret: str,
    scope: str = "https://mail.google.com/",
    redirect_host: str = "127.0.0.1",
    redirect_port: int = 53682,
    callback_path: str = "/oauth2/callback",
    timeout_seconds: int = 240,
) -> GoogleOAuthTokens:
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://{redirect_host}:{redirect_port}{callback_path}"
    auth_query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    authorize_url = f"{GOOGLE_AUTHORIZE_URL}?{urlencode(auth_query)}"

    print("Opening browser for Google consent...")
    opened = webbrowser.open(authorize_url, new=1, autoraise=True)
    if not opened:
        print("Could not auto-open browser. Open this URL manually:")
    print(authorize_url)

    code = _wait_for_google_callback_code(
        redirect_host=redirect_host,
        redirect_port=redirect_port,
        callback_path=callback_path,
        expected_state=state,
        timeout_seconds=timeout_seconds,
    )

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    data = _post_form(GOOGLE_TOKEN_URL, payload, timeout_seconds=30)
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError("Google token endpoint did not return access_token.")

    refresh_token = data.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        refresh_token = None

    expires_in_raw = data.get("expires_in")
    expires_in = int(expires_in_raw) if isinstance(expires_in_raw, int) else None
    token_type = data.get("token_type")
    scope_value = data.get("scope")

    return GoogleOAuthTokens(
        access_token=access_token.strip(),
        refresh_token=refresh_token.strip() if refresh_token else None,
        token_type=token_type.strip() if isinstance(token_type, str) else None,
        scope=scope_value.strip() if isinstance(scope_value, str) else None,
        expires_in=expires_in,
    )


def upsert_env_file(env_path: Path, values: dict[str, str]) -> None:
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    index_by_key: dict[str, int] = {}
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            index_by_key[key] = idx

    for key, value in values.items():
        safe_value = value.replace("\n", "").strip()
        rendered = f"{key}={safe_value}"
        if key in index_by_key:
            lines[index_by_key[key]] = rendered
        else:
            lines.append(rendered)

    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
