from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from invoice_tracker.dashboard import run_dashboard
from invoice_tracker.oauth import bootstrap_google_oauth_tokens, upsert_env_file
from invoice_tracker.pipeline import run_pipeline


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="AI-powered invoice and receipt extractor from IMAP inbox."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to YAML configuration file (default: config.yaml).",
    )
    parser.add_argument(
        "--setup-gmail-oauth",
        action="store_true",
        help=(
            "Open browser and complete Gmail OAuth, then fetch access/refresh tokens."
        ),
    )
    parser.add_argument(
        "--google-client-id-env",
        default="GOOGLE_CLIENT_ID",
        help="Env var name containing Google OAuth client ID.",
    )
    parser.add_argument(
        "--google-client-secret-env",
        default="GOOGLE_CLIENT_SECRET",
        help="Env var name containing Google OAuth client secret.",
    )
    parser.add_argument(
        "--imap-access-token-env",
        default="IMAP_ACCESS_TOKEN",
        help="Target env var name for IMAP OAuth access token.",
    )
    parser.add_argument(
        "--imap-refresh-token-env",
        default="GOOGLE_REFRESH_TOKEN",
        help="Target env var name for IMAP OAuth refresh token.",
    )
    parser.add_argument(
        "--gmail-scope",
        default="https://mail.google.com/",
        help="OAuth scope for Gmail IMAP.",
    )
    parser.add_argument(
        "--redirect-host",
        default="127.0.0.1",
        help="Local callback host for OAuth redirect.",
    )
    parser.add_argument(
        "--redirect-port",
        type=int,
        default=53682,
        help="Local callback port for OAuth redirect.",
    )
    parser.add_argument(
        "--oauth-timeout-seconds",
        type=int,
        default=240,
        help="Timeout waiting for OAuth callback in browser flow.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to .env file where tokens should be written.",
    )
    parser.add_argument(
        "--no-save-env",
        action="store_true",
        help="Do not write retrieved tokens to .env.",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Run the local dashboard to browse extracted invoice data.",
    )
    parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="Host interface for dashboard server (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8787,
        help="Port for dashboard server (default: 8787).",
    )
    args = parser.parse_args()

    if args.setup_gmail_oauth:
        client_id = os.getenv(args.google_client_id_env, "").strip()
        client_secret = os.getenv(args.google_client_secret_env, "").strip()
        if not client_id:
            raise ValueError(
                f"Missing Google client ID in env '{args.google_client_id_env}'."
            )
        if not client_secret:
            raise ValueError(
                f"Missing Google client secret in env "
                f"'{args.google_client_secret_env}'."
            )

        tokens = bootstrap_google_oauth_tokens(
            client_id=client_id,
            client_secret=client_secret,
            scope=args.gmail_scope,
            redirect_host=args.redirect_host,
            redirect_port=args.redirect_port,
            timeout_seconds=args.oauth_timeout_seconds,
        )

        print("Google OAuth token exchange succeeded.")
        print(f"{args.imap_access_token_env}={tokens.access_token}")
        if tokens.refresh_token:
            print(f"{args.imap_refresh_token_env}={tokens.refresh_token}")
        else:
            print(
                "Refresh token was not returned. You may need to revoke previous "
                "grants and retry."
            )

        if not args.no_save_env:
            updates = {args.imap_access_token_env: tokens.access_token}
            if tokens.refresh_token:
                updates[args.imap_refresh_token_env] = tokens.refresh_token
            upsert_env_file(args.env_file, updates)
            print(f"Saved OAuth tokens to {args.env_file.resolve()}")
        return

    if args.dashboard:
        run_dashboard(
            args.config,
            host=args.dashboard_host,
            port=args.dashboard_port,
        )
        return

    run_pipeline(args.config)


if __name__ == "__main__":
    main()
