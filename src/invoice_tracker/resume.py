from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ResumeStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict:
        if not self._path.exists():
            return {"mailboxes": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {"mailboxes": {}}
        if not isinstance(data, dict):
            return {"mailboxes": {}}
        mailboxes = data.get("mailboxes")
        if not isinstance(mailboxes, dict):
            data["mailboxes"] = {}
        return data

    def get_last_uid(self, mailbox: str) -> int | None:
        data = self._load()
        mailbox_data = data.get("mailboxes", {}).get(mailbox)
        if not isinstance(mailbox_data, dict):
            return None
        value = mailbox_data.get("last_uid")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    def set_last_uid(self, mailbox: str, uid: int) -> None:
        data = self._load()
        mailboxes = data.setdefault("mailboxes", {})
        mailboxes[mailbox] = {
            "last_uid": int(uid),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        temp_path.replace(self._path)
