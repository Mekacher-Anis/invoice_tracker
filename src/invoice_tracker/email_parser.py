from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import fitz
from pypdf import PdfReader

LINK_REGEX = re.compile(r"https?://[^\s<>'\"()]+", flags=re.IGNORECASE)
HREF_REGEX = re.compile(r"""href=["'](https?://[^"']+)["']""", flags=re.IGNORECASE)
SAFE_FILENAME_REGEX = re.compile(r"[^A-Za-z0-9._-]+")
DOWNLOAD_FILE_EXTENSIONS = {
    ".pdf",
    ".zip",
    ".csv",
    ".xml",
    ".txt",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
}
DIRECT_LINK_HINTS = ("download", "invoice", "receipt", "attachment", "file")
REPLY_SUBJECT_PREFIX_REGEX = re.compile(
    r"^\s*(?:(?:re|aw|sv|antw|fw|fwd)\s*:\s*)+",
    flags=re.IGNORECASE,
)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self.parts.append(stripped)

    def text(self) -> str:
        return " ".join(self.parts)


@dataclass
class ParsedLink:
    url: str
    kind: str


@dataclass
class ParsedDocument:
    kind: str
    filename: str
    file_path: Path
    mime_type: str | None
    source_link: str | None = None
    extracted_text: str = ""
    page_image_paths: list[Path] = field(default_factory=list)


@dataclass
class ParsedEmail:
    imap_uid: str
    message_id: str | None
    subject: str
    sender: str
    recipients: str
    sent_at: str | None
    is_reply: bool
    body_text: str
    raw_email_path: Path
    documents: list[ParsedDocument] = field(default_factory=list)
    links: list[ParsedLink] = field(default_factory=list)
    pdf_text: str = ""
    pdf_page_images: list[Path] = field(default_factory=list)


@dataclass
class EmailPreview:
    subject: str
    sender: str
    recipients: str
    sent_at: str | None
    is_reply: bool
    body_text: str


def _sanitize_filename(name: str) -> str:
    cleaned = SAFE_FILENAME_REGEX.sub("_", name.strip())
    return cleaned or "file.bin"


def _dedupe_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _to_iso_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.isoformat()
    except Exception:
        return None


def _is_reply_email(
    *,
    subject: str,
    in_reply_to: str | None,
) -> bool:
    if in_reply_to and in_reply_to.strip():
        return True
    return REPLY_SUBJECT_PREFIX_REGEX.match(subject or "") is not None


def _part_text(part) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            return content.decode(part.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        pass

    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _html_to_text(html_text: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html_text)
    parser.close()
    return unescape(parser.text())


def _extract_links_from_text(text: str) -> list[str]:
    return [match.group(0).rstrip(".,);") for match in LINK_REGEX.finditer(text)]


def _extract_links_from_html(html_text: str) -> list[str]:
    return [match.group(1) for match in HREF_REGEX.finditer(html_text)]


def _unique_in_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _classify_link(url: str, dashboard_keywords: list[str]) -> str:
    lower = url.lower()
    parsed = urlparse(url)
    suffix = Path(unquote(parsed.path)).suffix.lower()

    if suffix in DOWNLOAD_FILE_EXTENSIONS:
        return "direct_download"
    if any(hint in lower for hint in DIRECT_LINK_HINTS):
        return "direct_download"
    if any(keyword in lower for keyword in dashboard_keywords):
        return "dashboard"
    return "other"


def _extract_pdf_text(path: Path, max_chars: int) -> str:
    try:
        reader = PdfReader(str(path))
        parts: list[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text:
                parts.append(page_text)
        return "\n".join(parts)[:max_chars]
    except Exception:
        return ""


def _render_pdf_pages_to_images(
    *,
    pdf_path: Path,
    dpi: int,
    max_pages: int,
) -> list[Path]:
    image_paths: list[Path] = []
    try:
        document = fitz.open(str(pdf_path))
    except Exception:
        return image_paths

    scale = max(1.0, dpi / 72.0)
    matrix = fitz.Matrix(scale, scale)
    page_count = min(document.page_count, max_pages)
    output_dir = pdf_path.parent / f"{pdf_path.stem}_pages"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        for page_index in range(page_count):
            page = document.load_page(page_index)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image_path = output_dir / f"page_{page_index + 1:03d}.png"
            pix.save(str(image_path))
            image_paths.append(image_path)
    finally:
        document.close()

    return image_paths


def _download_link(
    *,
    url: str,
    destination_dir: Path,
    user_agent: str,
    timeout_seconds: int,
) -> tuple[Path, str | None] | None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": user_agent})

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type")
    except (URLError, HTTPError, TimeoutError):
        return None
    except Exception:
        return None

    parsed = urlparse(url)
    filename = _sanitize_filename(Path(unquote(parsed.path)).name or "downloaded.bin")
    target = _dedupe_path(destination_dir / filename)
    target.write_bytes(data)
    return target, content_type


def parse_email(
    *,
    imap_uid: str,
    mailbox: str,
    raw_email: bytes,
    raw_email_dir: Path,
    attachments_dir: Path,
    downloads_dir: Path,
    allow_link_download: bool,
    dashboard_keywords: list[str],
    pdf_char_limit: int,
    pdf_image_dpi: int,
    max_pdf_pages_for_llm: int,
    user_agent: str,
    download_timeout_seconds: int,
) -> ParsedEmail:
    raw_email_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    mailbox_uid = _sanitize_filename(f"{mailbox}_{imap_uid}")
    raw_email_path = _dedupe_path(raw_email_dir / f"{mailbox_uid}.eml")
    raw_email_path.write_bytes(raw_email)

    message = BytesParser(policy=policy.default).parsebytes(raw_email)
    subject = str(message.get("Subject", "")).strip()
    sender = str(message.get("From", "")).strip()
    recipients = str(message.get("To", "")).strip()
    message_id = str(message.get("Message-Id", "")).strip() or None
    sent_at = _to_iso_datetime(message.get("Date"))
    in_reply_to = str(message.get("In-Reply-To", "")).strip() or None
    is_reply = _is_reply_email(subject=subject, in_reply_to=in_reply_to)

    plain_parts: list[str] = []
    html_parts: list[str] = []
    documents: list[ParsedDocument] = []

    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            content_disposition = (part.get_content_disposition() or "").lower()
            content_type = part.get_content_type()
            filename = part.get_filename()

            if filename or content_disposition == "attachment":
                payload = part.get_payload(decode=True) or b""
                if not payload:
                    continue
                attachment_dir = attachments_dir / mailbox_uid
                attachment_dir.mkdir(parents=True, exist_ok=True)
                safe_name = _sanitize_filename(filename or "attachment.bin")
                path = _dedupe_path(attachment_dir / safe_name)
                path.write_bytes(payload)

                extracted_text = ""
                page_image_paths: list[Path] = []
                if path.suffix.lower() == ".pdf" or content_type == "application/pdf":
                    extracted_text = _extract_pdf_text(path, pdf_char_limit)
                    page_image_paths = _render_pdf_pages_to_images(
                        pdf_path=path,
                        dpi=pdf_image_dpi,
                        max_pages=max_pdf_pages_for_llm,
                    )

                documents.append(
                    ParsedDocument(
                        kind="attachment",
                        filename=path.name,
                        file_path=path,
                        mime_type=content_type,
                        extracted_text=extracted_text,
                        page_image_paths=page_image_paths,
                    )
                )
                continue

            if content_type == "text/plain":
                plain_parts.append(_part_text(part))
            elif content_type == "text/html":
                html_parts.append(_part_text(part))
    else:
        content_type = message.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(_part_text(message))
        elif content_type == "text/html":
            html_parts.append(_part_text(message))

    html_text = "\n\n".join(_html_to_text(x) for x in html_parts if x.strip())
    body_text = "\n\n".join([*plain_parts, html_text]).strip()

    link_candidates: list[str] = []
    link_candidates.extend(_extract_links_from_text(body_text))
    for html_body in html_parts:
        link_candidates.extend(_extract_links_from_html(html_body))
        link_candidates.extend(_extract_links_from_text(html_body))

    links = _unique_in_order([link for link in link_candidates if link])
    parsed_links = [
        ParsedLink(url=url, kind=_classify_link(url, dashboard_keywords))
        for url in links
    ]

    has_pdf = any(doc.file_path.suffix.lower() == ".pdf" for doc in documents)
    if not has_pdf and allow_link_download:
        direct_links = [link.url for link in parsed_links if link.kind == "direct_download"]
        download_dir = downloads_dir / mailbox_uid
        for url in direct_links:
            downloaded = _download_link(
                url=url,
                destination_dir=download_dir,
                user_agent=user_agent,
                timeout_seconds=download_timeout_seconds,
            )
            if downloaded is None:
                continue
            path, content_type = downloaded
            extracted_text = ""
            page_image_paths: list[Path] = []
            if path.suffix.lower() == ".pdf" or (content_type and "pdf" in content_type):
                extracted_text = _extract_pdf_text(path, pdf_char_limit)
                page_image_paths = _render_pdf_pages_to_images(
                    pdf_path=path,
                    dpi=pdf_image_dpi,
                    max_pages=max_pdf_pages_for_llm,
                )
            documents.append(
                ParsedDocument(
                    kind="download",
                    filename=path.name,
                    file_path=path,
                    mime_type=content_type,
                    source_link=url,
                    extracted_text=extracted_text,
                    page_image_paths=page_image_paths,
                )
            )

    pdf_text_parts = [doc.extracted_text for doc in documents if doc.extracted_text]
    pdf_text = "\n\n".join(pdf_text_parts).strip()[:pdf_char_limit]
    pdf_page_images: list[Path] = []
    for document in documents:
        pdf_page_images.extend(document.page_image_paths)

    return ParsedEmail(
        imap_uid=imap_uid,
        message_id=message_id,
        subject=subject,
        sender=sender,
        recipients=recipients,
        sent_at=sent_at,
        is_reply=is_reply,
        body_text=body_text,
        raw_email_path=raw_email_path,
        documents=documents,
        links=parsed_links,
        pdf_text=pdf_text,
        pdf_page_images=pdf_page_images,
    )


def preview_email_for_classification(
    *,
    raw_email: bytes,
    body_char_limit: int,
) -> EmailPreview:
    message = BytesParser(policy=policy.default).parsebytes(raw_email)
    subject = str(message.get("Subject", "")).strip()
    sender = str(message.get("From", "")).strip()
    recipients = str(message.get("To", "")).strip()
    sent_at = _to_iso_datetime(message.get("Date"))
    in_reply_to = str(message.get("In-Reply-To", "")).strip() or None
    is_reply = _is_reply_email(subject=subject, in_reply_to=in_reply_to)

    plain_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue

            content_disposition = (part.get_content_disposition() or "").lower()
            if content_disposition == "attachment" or part.get_filename():
                continue

            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(_part_text(part))
            elif content_type == "text/html":
                html_parts.append(_part_text(part))
    else:
        content_type = message.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(_part_text(message))
        elif content_type == "text/html":
            html_parts.append(_part_text(message))

    html_text = "\n\n".join(_html_to_text(x) for x in html_parts if x.strip())
    body_text = "\n\n".join([*plain_parts, html_text]).strip()[:body_char_limit]

    return EmailPreview(
        subject=subject,
        sender=sender,
        recipients=recipients,
        sent_at=sent_at,
        is_reply=is_reply,
        body_text=body_text,
    )
