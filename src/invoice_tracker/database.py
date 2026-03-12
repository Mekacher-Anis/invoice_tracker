from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class InvoiceDatabase:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(database_path)
        self._conn.execute("PRAGMA foreign_keys = ON")

    def init_schema(self) -> None:
        cursor = self._conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox TEXT NOT NULL,
                imap_uid TEXT NOT NULL,
                message_id TEXT,
                subject TEXT,
                sender TEXT,
                recipients TEXT,
                sent_at TEXT,
                body_text TEXT,
                raw_email_path TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(mailbox, imap_uid)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                mime_type TEXT,
                source_link TEXT,
                extracted_text TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS extractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER NOT NULL UNIQUE,
                is_invoice INTEGER NOT NULL,
                is_receipt INTEGER NOT NULL,
                invoice_date TEXT,
                product TEXT,
                company TEXT,
                price REAL,
                currency TEXT,
                vat REAL,
                confidence REAL,
                source_used TEXT,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
            );
            """
        )
        self._conn.commit()

    def upsert_email(
        self,
        *,
        mailbox: str,
        imap_uid: str,
        message_id: str | None,
        subject: str,
        sender: str,
        recipients: str,
        sent_at: str | None,
        body_text: str,
        raw_email_path: str,
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        cursor = self._conn.cursor()
        existing = cursor.execute(
            "SELECT id FROM emails WHERE mailbox = ? AND imap_uid = ?",
            (mailbox, imap_uid),
        ).fetchone()

        if existing:
            email_id = int(existing[0])
            cursor.execute(
                """
                UPDATE emails
                SET message_id = ?,
                    subject = ?,
                    sender = ?,
                    recipients = ?,
                    sent_at = ?,
                    body_text = ?,
                    raw_email_path = ?
                WHERE id = ?
                """,
                (
                    message_id,
                    subject,
                    sender,
                    recipients,
                    sent_at,
                    body_text,
                    raw_email_path,
                    email_id,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO emails (
                    mailbox, imap_uid, message_id, subject, sender, recipients,
                    sent_at, body_text, raw_email_path, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mailbox,
                    imap_uid,
                    message_id,
                    subject,
                    sender,
                    recipients,
                    sent_at,
                    body_text,
                    raw_email_path,
                    created_at,
                ),
            )
            email_id = int(cursor.lastrowid)

        self._conn.commit()
        return email_id

    def clear_email_children(self, email_id: int) -> None:
        cursor = self._conn.cursor()
        cursor.execute("DELETE FROM documents WHERE email_id = ?", (email_id,))
        cursor.execute("DELETE FROM links WHERE email_id = ?", (email_id,))
        cursor.execute("DELETE FROM extractions WHERE email_id = ?", (email_id,))
        self._conn.commit()

    def insert_document(
        self,
        *,
        email_id: int,
        kind: str,
        filename: str,
        file_path: str,
        mime_type: str | None,
        source_link: str | None,
        extracted_text: str,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO documents (
                email_id, kind, filename, file_path, mime_type, source_link,
                extracted_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_id,
                kind,
                filename,
                file_path,
                mime_type,
                source_link,
                extracted_text,
                created_at,
            ),
        )
        self._conn.commit()

    def insert_link(self, *, email_id: int, url: str, kind: str) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO links (email_id, url, kind, created_at) VALUES (?, ?, ?, ?)",
            (email_id, url, kind, created_at),
        )
        self._conn.commit()

    def insert_extraction(
        self,
        *,
        email_id: int,
        is_invoice: bool,
        is_receipt: bool,
        invoice_date: str | None,
        product: str | None,
        company: str | None,
        price: float | None,
        currency: str | None,
        vat: float | None,
        confidence: float,
        source_used: str,
        raw_json: dict,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO extractions (
                email_id, is_invoice, is_receipt, invoice_date, product, company,
                price, currency, vat, confidence, source_used, raw_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_id,
                int(is_invoice),
                int(is_receipt),
                invoice_date,
                product,
                company,
                price,
                currency,
                vat,
                confidence,
                source_used,
                json.dumps(raw_json, ensure_ascii=True),
                created_at,
            ),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
