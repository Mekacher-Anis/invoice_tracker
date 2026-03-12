from __future__ import annotations

import json
import mimetypes
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import load_config

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200

STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class DashboardError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_cls: type[BaseHTTPRequestHandler],
        *,
        db_path: Path,
        web_dir: Path,
    ) -> None:
        super().__init__(server_address, request_handler_cls)
        self.db_path = db_path
        self.web_dir = web_dir


def _first_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if not values:
        return default
    return values[0].strip()


def _coerce_int(
    value: str | None,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    text = (value or "").strip()
    if not text:
        number = default
    else:
        try:
            number = int(text)
        except ValueError as exc:
            raise DashboardError(HTTPStatus.BAD_REQUEST, f"Invalid integer: '{text}'.") from exc

    if minimum is not None and number < minimum:
        number = minimum
    if maximum is not None and number > maximum:
        number = maximum
    return number


def _serialize_record_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "email_id": int(row["email_id"]),
        "imap_uid": row["imap_uid"],
        "subject": row["subject"],
        "sender": row["sender"],
        "recipients": row["recipients"],
        "sent_at": row["sent_at"],
        "invoice_date": row["invoice_date"],
        "product": row["product"],
        "company": row["company"],
        "price": row["price"],
        "currency": row["currency"],
        "vat": row["vat"],
        "is_invoice": bool(row["is_invoice"]),
        "is_receipt": bool(row["is_receipt"]),
        "confidence": row["confidence"],
        "source_used": row["source_used"],
        "document_count": int(row["document_count"]),
        "link_count": int(row["link_count"]),
        "extracted_at": row["extracted_at"],
    }


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path in STATIC_ROUTES:
                asset_name, content_type = STATIC_ROUTES[path]
                self._serve_static(asset_name, content_type)
                return

            if path.startswith("/api/"):
                self._serve_api(path, query)
                return

            raise DashboardError(HTTPStatus.NOT_FOUND, "Not found.")
        except DashboardError as exc:
            self._send_json({"error": exc.message}, status=exc.status)
        except sqlite3.Error as exc:
            self._send_json(
                {"error": f"Database error: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        except Exception:
            self._send_json(
                {"error": "Internal server error."},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _open_db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.server.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = (self.server.web_dir / filename).resolve()
        if not path.exists() or not path.is_file():
            raise DashboardError(HTTPStatus.NOT_FOUND, f"Asset not found: {filename}")

        payload = path.read_bytes()
        self._send_bytes(
            payload,
            status=HTTPStatus.OK,
            content_type=content_type,
            extra_headers={"Cache-Control": "no-cache"},
        )

    def _serve_api(self, path: str, query: dict[str, list[str]]) -> None:
        path_parts = [segment for segment in path.split("/") if segment]

        if path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            return

        with self._open_db() as connection:
            if path == "/api/summary":
                self._send_json(self._build_summary(connection))
                return

            if path == "/api/options":
                self._send_json(self._build_options(connection))
                return

            if path == "/api/records":
                self._send_json(self._build_records(connection, query))
                return

            if len(path_parts) == 3 and path_parts[:2] == ["api", "records"]:
                email_id = _coerce_int(path_parts[2], default=0, minimum=1)
                self._send_json(self._build_record_detail(connection, email_id))
                return

            if (
                len(path_parts) == 4
                and path_parts[0] == "api"
                and path_parts[1] == "documents"
                and path_parts[3] == "file"
            ):
                document_id = _coerce_int(path_parts[2], default=0, minimum=1)
                self._serve_document_file(connection, document_id)
                return

            if (
                len(path_parts) == 4
                and path_parts[0] == "api"
                and path_parts[1] == "emails"
                and path_parts[3] == "raw"
            ):
                email_id = _coerce_int(path_parts[2], default=0, minimum=1)
                self._serve_raw_email_file(connection, email_id)
                return

        raise DashboardError(HTTPStatus.NOT_FOUND, "API route not found.")

    def _build_summary(self, connection: sqlite3.Connection) -> dict[str, Any]:
        totals = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM emails) AS emails,
                (SELECT COUNT(*) FROM extractions) AS classified,
                (SELECT COUNT(*) FROM extractions WHERE is_invoice = 1) AS invoices,
                (SELECT COUNT(*) FROM extractions WHERE is_receipt = 1) AS receipts,
                (SELECT COUNT(*) FROM extractions WHERE is_invoice = 1 AND is_receipt = 1) AS both,
                (SELECT COUNT(*) FROM documents) AS documents,
                (SELECT COUNT(*) FROM links) AS links
            """
        ).fetchone()

        spend_by_currency = [
            {
                "currency": row["currency"],
                "count": int(row["count"]),
                "total": row["total"],
            }
            for row in connection.execute(
                """
                SELECT
                    COALESCE(NULLIF(UPPER(currency), ''), '(none)') AS currency,
                    COUNT(*) AS count,
                    ROUND(SUM(COALESCE(price, 0)), 2) AS total
                FROM extractions
                GROUP BY 1
                ORDER BY count DESC, currency ASC
                """
            ).fetchall()
        ]

        top_companies = [
            {
                "company": row["company"],
                "count": int(row["count"]),
                "total": row["total"],
            }
            for row in connection.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(company), ''), '(unknown)') AS company,
                    COUNT(*) AS count,
                    ROUND(SUM(COALESCE(price, 0)), 2) AS total
                FROM extractions
                GROUP BY 1
                ORDER BY count DESC, company ASC
                LIMIT 10
                """
            ).fetchall()
        ]

        monthly = [
            {
                "month": row["month"],
                "count": int(row["count"]),
                "total": row["total"],
            }
            for row in connection.execute(
                """
                SELECT
                    SUBSTR(
                        COALESCE(
                            NULLIF(ex.invoice_date, ''),
                            NULLIF(SUBSTR(em.sent_at, 1, 10), '')
                        ),
                        1,
                        7
                    ) AS month,
                    COUNT(*) AS count,
                    ROUND(SUM(COALESCE(ex.price, 0)), 2) AS total
                FROM extractions ex
                JOIN emails em ON em.id = ex.email_id
                GROUP BY month
                HAVING month IS NOT NULL AND month <> ''
                ORDER BY month ASC
                """
            ).fetchall()
        ]

        recent_rows = connection.execute(
            """
            SELECT
                em.id AS email_id,
                em.imap_uid,
                em.subject,
                em.sender,
                em.recipients,
                em.sent_at,
                ex.invoice_date,
                ex.product,
                ex.company,
                ex.price,
                ex.currency,
                ex.vat,
                ex.is_invoice,
                ex.is_receipt,
                ex.confidence,
                ex.source_used,
                ex.created_at AS extracted_at,
                COALESCE(doc_counts.document_count, 0) AS document_count,
                COALESCE(link_counts.link_count, 0) AS link_count
            FROM extractions ex
            JOIN emails em ON em.id = ex.email_id
            LEFT JOIN (
                SELECT email_id, COUNT(*) AS document_count
                FROM documents
                GROUP BY email_id
            ) AS doc_counts ON doc_counts.email_id = em.id
            LEFT JOIN (
                SELECT email_id, COUNT(*) AS link_count
                FROM links
                GROUP BY email_id
            ) AS link_counts ON link_counts.email_id = em.id
            ORDER BY COALESCE(NULLIF(ex.invoice_date, ''), SUBSTR(em.sent_at, 1, 10), ex.created_at) DESC,
                     em.id DESC
            LIMIT 8
            """
        ).fetchall()

        return {
            "totals": dict(totals) if totals is not None else {},
            "spend_by_currency": spend_by_currency,
            "top_companies": top_companies,
            "monthly": monthly,
            "recent": [_serialize_record_row(row) for row in recent_rows],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _build_options(self, connection: sqlite3.Connection) -> dict[str, Any]:
        currencies = [
            row["currency"]
            for row in connection.execute(
                """
                SELECT DISTINCT UPPER(currency) AS currency
                FROM extractions
                WHERE currency IS NOT NULL AND TRIM(currency) <> ''
                ORDER BY currency ASC
                """
            ).fetchall()
        ]

        companies = [
            row["company"]
            for row in connection.execute(
                """
                SELECT DISTINCT TRIM(company) AS company
                FROM extractions
                WHERE company IS NOT NULL AND TRIM(company) <> ''
                ORDER BY LOWER(company) ASC
                """
            ).fetchall()
        ]

        return {"currencies": currencies, "companies": companies}

    def _build_records(
        self,
        connection: sqlite3.Connection,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        search = _first_value(query, "search")
        kind = _first_value(query, "kind", "all").lower()
        currency = _first_value(query, "currency").upper()
        company = _first_value(query, "company")
        sort = _first_value(query, "sort", "newest").lower()

        limit = _coerce_int(
            _first_value(query, "limit"),
            default=DEFAULT_PAGE_SIZE,
            minimum=1,
            maximum=MAX_PAGE_SIZE,
        )
        offset = _coerce_int(
            _first_value(query, "offset"),
            default=0,
            minimum=0,
        )

        where_parts: list[str] = []
        parameters: list[Any] = []

        if kind == "invoice":
            where_parts.append("ex.is_invoice = 1")
        elif kind == "receipt":
            where_parts.append("ex.is_receipt = 1")
        elif kind == "both":
            where_parts.append("ex.is_invoice = 1 AND ex.is_receipt = 1")
        elif kind not in {"", "all"}:
            raise DashboardError(
                HTTPStatus.BAD_REQUEST,
                "Invalid filter for 'kind'. Expected all|invoice|receipt|both.",
            )

        if currency:
            where_parts.append("UPPER(COALESCE(ex.currency, '')) = ?")
            parameters.append(currency)

        if company:
            where_parts.append("LOWER(COALESCE(ex.company, '')) = LOWER(?)")
            parameters.append(company)

        if search:
            pattern = f"%{search.lower()}%"
            where_parts.append(
                """
                (
                    LOWER(COALESCE(em.subject, '')) LIKE ?
                    OR LOWER(COALESCE(em.sender, '')) LIKE ?
                    OR LOWER(COALESCE(ex.product, '')) LIKE ?
                    OR LOWER(COALESCE(ex.company, '')) LIKE ?
                    OR LOWER(COALESCE(ex.currency, '')) LIKE ?
                    OR LOWER(COALESCE(em.imap_uid, '')) LIKE ?
                )
                """
            )
            parameters.extend([pattern, pattern, pattern, pattern, pattern, pattern])

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        count_sql = f"""
            SELECT COUNT(*) AS total
            FROM extractions ex
            JOIN emails em ON em.id = ex.email_id
            {where_sql}
        """
        total_row = connection.execute(count_sql, parameters).fetchone()
        total = int(total_row["total"]) if total_row is not None else 0

        sort_sql_map = {
            "newest": (
                "ORDER BY COALESCE(NULLIF(ex.invoice_date, ''), SUBSTR(em.sent_at, 1, 10), ex.created_at) DESC, "
                "em.id DESC"
            ),
            "oldest": (
                "ORDER BY COALESCE(NULLIF(ex.invoice_date, ''), SUBSTR(em.sent_at, 1, 10), ex.created_at) ASC, "
                "em.id ASC"
            ),
            "amount_desc": "ORDER BY COALESCE(ex.price, 0) DESC, em.id DESC",
            "amount_asc": "ORDER BY COALESCE(ex.price, 0) ASC, em.id ASC",
            "confidence_desc": "ORDER BY COALESCE(ex.confidence, 0) DESC, em.id DESC",
        }
        sort_sql = sort_sql_map.get(sort, sort_sql_map["newest"])

        list_sql = f"""
            SELECT
                em.id AS email_id,
                em.imap_uid,
                em.subject,
                em.sender,
                em.recipients,
                em.sent_at,
                ex.invoice_date,
                ex.product,
                ex.company,
                ex.price,
                ex.currency,
                ex.vat,
                ex.is_invoice,
                ex.is_receipt,
                ex.confidence,
                ex.source_used,
                ex.created_at AS extracted_at,
                COALESCE(doc_counts.document_count, 0) AS document_count,
                COALESCE(link_counts.link_count, 0) AS link_count
            FROM extractions ex
            JOIN emails em ON em.id = ex.email_id
            LEFT JOIN (
                SELECT email_id, COUNT(*) AS document_count
                FROM documents
                GROUP BY email_id
            ) AS doc_counts ON doc_counts.email_id = em.id
            LEFT JOIN (
                SELECT email_id, COUNT(*) AS link_count
                FROM links
                GROUP BY email_id
            ) AS link_counts ON link_counts.email_id = em.id
            {where_sql}
            {sort_sql}
            LIMIT ? OFFSET ?
        """

        rows = connection.execute(list_sql, [*parameters, limit, offset]).fetchall()
        records = [_serialize_record_row(row) for row in rows]

        return {
            "records": records,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
            },
        }

    def _build_record_detail(
        self,
        connection: sqlite3.Connection,
        email_id: int,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT
                em.id AS email_id,
                em.imap_uid,
                em.message_id,
                em.subject,
                em.sender,
                em.recipients,
                em.sent_at,
                em.body_text,
                em.raw_email_path,
                ex.invoice_date,
                ex.product,
                ex.company,
                ex.price,
                ex.currency,
                ex.vat,
                ex.is_invoice,
                ex.is_receipt,
                ex.confidence,
                ex.source_used,
                ex.raw_json,
                ex.created_at AS extracted_at
            FROM extractions ex
            JOIN emails em ON em.id = ex.email_id
            WHERE em.id = ?
            """,
            (email_id,),
        ).fetchone()

        if row is None:
            raise DashboardError(HTTPStatus.NOT_FOUND, f"Record not found for email_id={email_id}.")

        document_rows = connection.execute(
            """
            SELECT id, kind, filename, file_path, mime_type, source_link, created_at
            FROM documents
            WHERE email_id = ?
            ORDER BY id DESC
            """,
            (email_id,),
        ).fetchall()
        documents = [
            {
                "id": int(doc["id"]),
                "kind": doc["kind"],
                "filename": doc["filename"],
                "mime_type": doc["mime_type"],
                "source_link": doc["source_link"],
                "file_path": doc["file_path"],
                "created_at": doc["created_at"],
                "download_url": f"/api/documents/{int(doc['id'])}/file",
            }
            for doc in document_rows
        ]

        link_rows = connection.execute(
            """
            SELECT id, url, kind, created_at
            FROM links
            WHERE email_id = ?
            ORDER BY id DESC
            """,
            (email_id,),
        ).fetchall()
        links = [
            {
                "id": int(link["id"]),
                "url": link["url"],
                "kind": link["kind"],
                "created_at": link["created_at"],
            }
            for link in link_rows
        ]

        raw_json_payload = row["raw_json"] or "{}"
        try:
            raw_json = json.loads(raw_json_payload)
        except json.JSONDecodeError:
            raw_json = {"raw_json_unparsed": raw_json_payload}

        record = {
            "email_id": int(row["email_id"]),
            "imap_uid": row["imap_uid"],
            "message_id": row["message_id"],
            "subject": row["subject"],
            "sender": row["sender"],
            "recipients": row["recipients"],
            "sent_at": row["sent_at"],
            "body_text": row["body_text"],
            "raw_email_path": row["raw_email_path"],
            "raw_email_url": f"/api/emails/{int(row['email_id'])}/raw",
            "invoice_date": row["invoice_date"],
            "product": row["product"],
            "company": row["company"],
            "price": row["price"],
            "currency": row["currency"],
            "vat": row["vat"],
            "is_invoice": bool(row["is_invoice"]),
            "is_receipt": bool(row["is_receipt"]),
            "confidence": row["confidence"],
            "source_used": row["source_used"],
            "extracted_at": row["extracted_at"],
            "raw_json": raw_json,
        }

        return {
            "record": record,
            "documents": documents,
            "links": links,
        }

    def _serve_document_file(self, connection: sqlite3.Connection, document_id: int) -> None:
        row = connection.execute(
            """
            SELECT filename, file_path, mime_type
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            raise DashboardError(HTTPStatus.NOT_FOUND, f"Document {document_id} was not found.")

        file_path = Path(str(row["file_path"]))
        filename = str(row["filename"] or file_path.name)
        self._send_file(file_path, content_type=row["mime_type"], filename=filename)

    def _serve_raw_email_file(self, connection: sqlite3.Connection, email_id: int) -> None:
        row = connection.execute(
            """
            SELECT raw_email_path, imap_uid
            FROM emails
            WHERE id = ?
            """,
            (email_id,),
        ).fetchone()
        if row is None:
            raise DashboardError(HTTPStatus.NOT_FOUND, f"Email {email_id} was not found.")

        file_path = Path(str(row["raw_email_path"]))
        uid = str(row["imap_uid"] or email_id)
        self._send_file(
            file_path,
            content_type="message/rfc822",
            filename=f"email_{uid}.eml",
        )

    def _send_file(self, path: Path, *, content_type: str | None, filename: str) -> None:
        if not path.exists() or not path.is_file():
            raise DashboardError(HTTPStatus.NOT_FOUND, f"File not found: {path}")

        mime = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        safe_name = filename.replace('"', "_")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        self.end_headers()

        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _send_json(self, payload: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            raw,
            status=status,
            content_type="application/json; charset=utf-8",
            extra_headers={"Cache-Control": "no-cache"},
        )

    def _send_bytes(
        self,
        payload: bytes,
        *,
        status: HTTPStatus,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)


def run_dashboard(
    config_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    config = load_config(config_path)
    database_path = config.storage.database_path
    if not database_path.exists():
        raise FileNotFoundError(
            f"Dashboard database does not exist yet: {database_path}. "
            "Run the pipeline first."
        )

    web_dir = Path(__file__).resolve().parent / "web"
    if not web_dir.exists():
        raise FileNotFoundError(f"Dashboard static assets directory not found: {web_dir}")

    server = DashboardHTTPServer(
        (host, port),
        DashboardRequestHandler,
        db_path=database_path,
        web_dir=web_dir,
    )
    print(f"Dashboard available at http://{host}:{port}")
    print(f"Using database: {database_path}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
