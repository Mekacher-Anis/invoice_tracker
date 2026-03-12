from __future__ import annotations

import imaplib
from typing import Iterator

from .config import IMAPConfig


class IMAPInbox:
    def __init__(
        self,
        config: IMAPConfig,
        *,
        password: str | None = None,
        oauth_token: str | None = None,
    ) -> None:
        self._config = config
        self._password = password
        self._oauth_token = oauth_token
        self._imap: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "IMAPInbox":
        if self._config.use_ssl:
            self._imap = imaplib.IMAP4_SSL(self._config.host, self._config.port)
        else:
            self._imap = imaplib.IMAP4(self._config.host, self._config.port)

        if self._config.auth_method == "oauth2":
            if not self._oauth_token:
                raise ValueError("IMAP OAuth2 token is required but missing.")
            auth_string = (
                f"user={self._config.username}\x01"
                f"auth=Bearer {self._oauth_token}\x01\x01"
            )
            self._imap.authenticate(
                "XOAUTH2",
                lambda _: auth_string.encode("utf-8"),
            )
        else:
            if not self._password:
                raise ValueError("IMAP password is required but missing.")
            self._imap.login(self._config.username, self._password)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def fetch_messages(self) -> Iterator[tuple[str, bytes]]:
        if self._imap is None:
            raise RuntimeError("IMAP connection is not open.")

        status, _ = self._imap.select(self._config.mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select mailbox '{self._config.mailbox}'.")

        status, data = self._imap.search(None, self._config.search_criteria)
        if status != "OK":
            raise RuntimeError(
                f"Search failed for criteria '{self._config.search_criteria}'."
            )
        if not data or not data[0]:
            return

        uids = data[0].split()
        if self._config.max_messages is not None:
            uids = uids[-self._config.max_messages :]

        for uid in uids:
            fetch_status, message_data = self._imap.fetch(uid, "(RFC822)")
            if fetch_status != "OK" or not message_data:
                continue

            raw_bytes: bytes | None = None
            for part in message_data:
                if isinstance(part, tuple) and len(part) > 1:
                    raw_bytes = part[1]
                    break

            if raw_bytes:
                yield uid.decode(errors="ignore"), raw_bytes
