from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, load_config
from .database import InvoiceDatabase
from .email_client import IMAPInbox
from .email_parser import ParsedEmail, parse_email, preview_email_for_classification
from .extraction import ExtractionResult, InvoiceExtractor
from .oauth import resolve_imap_oauth_token
from .resume import ResumeStateStore


@dataclass
class MessageOutcome:
    uid: str
    uid_int: int | None
    parsed: ParsedEmail | None
    extraction: ExtractionResult | None
    skipped_non_invoice: bool = False
    skipped_reply: bool = False
    error: str | None = None


def _parse_uid_int(uid: str) -> int | None:
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


def _process_message(
    *,
    config: AppConfig,
    uid: str,
    raw_email: bytes,
    openai_api_key: str | None,
) -> MessageOutcome:
    uid_int = _parse_uid_int(uid)
    try:
        extractor = InvoiceExtractor(
            api_key=openai_api_key,
            base_url=config.openai.base_url,
            model=config.openai.model,
            temperature=config.openai.temperature,
            max_pdf_pages_for_llm=config.processing.max_pdf_pages_for_llm,
        )
        preview = preview_email_for_classification(
            raw_email=raw_email,
            body_char_limit=config.processing.body_char_limit,
        )
        if preview.is_reply:
            return MessageOutcome(
                uid=uid,
                uid_int=uid_int,
                parsed=None,
                extraction=None,
                skipped_reply=True,
            )

        precheck = extractor.extract(
            subject=preview.subject,
            sender=preview.sender,
            email_text=preview.body_text,
            pdf_text="",
        )
        if not (precheck.is_invoice or precheck.is_receipt):
            return MessageOutcome(
                uid=uid,
                uid_int=uid_int,
                parsed=None,
                extraction=None,
                skipped_non_invoice=True,
                error=None,
            )

        parsed = parse_email(
            imap_uid=uid,
            mailbox=config.imap.mailbox,
            raw_email=raw_email,
            raw_email_dir=config.storage.raw_email_dir,
            attachments_dir=config.storage.attachments_dir,
            downloads_dir=config.storage.downloads_dir,
            allow_link_download=config.processing.allow_link_download,
            dashboard_keywords=config.processing.dashboard_keywords,
            pdf_char_limit=config.processing.pdf_char_limit,
            pdf_image_dpi=config.processing.pdf_image_dpi,
            max_pdf_pages_for_llm=config.processing.max_pdf_pages_for_llm,
            user_agent=config.processing.user_agent,
            download_timeout_seconds=config.processing.download_timeout_seconds,
        )

        extraction = extractor.extract(
            subject=parsed.subject,
            sender=parsed.sender,
            email_text=parsed.body_text[: config.processing.body_char_limit],
            pdf_text=parsed.pdf_text[: config.processing.pdf_char_limit],
            pdf_page_images=parsed.pdf_page_images,
        )

        if not (extraction.is_invoice or extraction.is_receipt):
            return MessageOutcome(
                uid=uid,
                uid_int=uid_int,
                parsed=None,
                extraction=None,
                skipped_non_invoice=True,
                error=None,
            )

        return MessageOutcome(
            uid=uid,
            uid_int=uid_int,
            parsed=parsed,
            extraction=extraction,
            skipped_non_invoice=False,
            error=None,
        )
    except Exception as exc:
        return MessageOutcome(
            uid=uid,
            uid_int=uid_int,
            parsed=None,
            extraction=None,
            skipped_non_invoice=False,
            error=str(exc),
        )


def run_pipeline(config_path: Path) -> None:
    config = load_config(config_path)

    config.storage.attachments_dir.mkdir(parents=True, exist_ok=True)
    config.storage.downloads_dir.mkdir(parents=True, exist_ok=True)
    config.storage.raw_email_dir.mkdir(parents=True, exist_ok=True)

    database = InvoiceDatabase(config.storage.database_path)
    database.init_schema()

    imap_password: str | None = None
    imap_oauth_token: str | None = None
    if config.imap.auth_method == "oauth2":
        imap_oauth_token = resolve_imap_oauth_token(config)
    else:
        imap_password = config.get_imap_password()

    openai_api_key = config.get_openai_api_key()
    workers = max(1, config.processing.parallel_workers)
    max_in_flight = max(workers, config.processing.max_in_flight)

    resume_store = ResumeStateStore(config.processing.resume_state_path)
    resume_enabled = config.processing.resume_enabled
    resume_last_uid = (
        resume_store.get_last_uid(config.imap.mailbox) if resume_enabled else None
    )
    if resume_enabled:
        print(
            f"Resume enabled. mailbox={config.imap.mailbox}, "
            f"last_uid={resume_last_uid}"
        )

    processed = 0
    classified = 0
    failures = 0
    skipped_non_invoice = 0
    skipped_reply = 0
    skipped_resume = 0
    submitted = 0

    ordered_uids: list[int] = []
    uid_status: dict[int, str] = {}
    checkpoint_index = 0
    checkpoint_uid = resume_last_uid

    def advance_resume_checkpoint() -> None:
        nonlocal checkpoint_index, checkpoint_uid
        if not resume_enabled:
            return
        while checkpoint_index < len(ordered_uids):
            next_uid = ordered_uids[checkpoint_index]
            status = uid_status.get(next_uid)
            if status == "ok":
                checkpoint_uid = next_uid
                resume_store.set_last_uid(config.imap.mailbox, next_uid)
                checkpoint_index += 1
                continue
            if status in {"pending", None, "error"}:
                return

    def handle_outcome(outcome: MessageOutcome) -> None:
        nonlocal processed, classified, failures, skipped_non_invoice, skipped_reply

        tracked_uid = (
            resume_enabled
            and outcome.uid_int is not None
            and outcome.uid_int in uid_status
        )

        if outcome.error is not None:
            failures += 1
            print(f"[error] UID={outcome.uid} failed: {outcome.error}")
            if tracked_uid and outcome.uid_int is not None:
                uid_status[outcome.uid_int] = "error"
                advance_resume_checkpoint()
            return

        if outcome.skipped_reply:
            skipped_reply += 1
            print(f"[skip] UID={outcome.uid} reply/answer email")
            if tracked_uid and outcome.uid_int is not None:
                uid_status[outcome.uid_int] = "ok"
                advance_resume_checkpoint()
            return

        if outcome.skipped_non_invoice:
            skipped_non_invoice += 1
            print(f"[skip] UID={outcome.uid} not invoice/receipt")
            if tracked_uid and outcome.uid_int is not None:
                uid_status[outcome.uid_int] = "ok"
                advance_resume_checkpoint()
            return

        parsed = outcome.parsed
        result = outcome.extraction
        if parsed is None or result is None:
            failures += 1
            print(f"[error] UID={outcome.uid} failed: missing parsing/extraction output.")
            if tracked_uid and outcome.uid_int is not None:
                uid_status[outcome.uid_int] = "error"
                advance_resume_checkpoint()
            return

        try:
            email_id = database.upsert_email(
                mailbox=config.imap.mailbox,
                imap_uid=parsed.imap_uid,
                message_id=parsed.message_id,
                subject=parsed.subject,
                sender=parsed.sender,
                recipients=parsed.recipients,
                sent_at=parsed.sent_at,
                body_text=parsed.body_text,
                raw_email_path=str(parsed.raw_email_path),
            )
            database.clear_email_children(email_id)

            for document in parsed.documents:
                database.insert_document(
                    email_id=email_id,
                    kind=document.kind,
                    filename=document.filename,
                    file_path=str(document.file_path),
                    mime_type=document.mime_type,
                    source_link=document.source_link,
                    extracted_text=document.extracted_text,
                )
                for image_path in document.page_image_paths:
                    database.insert_document(
                        email_id=email_id,
                        kind="pdf_page_image",
                        filename=image_path.name,
                        file_path=str(image_path),
                        mime_type="image/png",
                        source_link=document.source_link,
                        extracted_text="",
                    )

            for link in parsed.links:
                database.insert_link(email_id=email_id, url=link.url, kind=link.kind)

            database.insert_extraction(
                email_id=email_id,
                is_invoice=result.is_invoice,
                is_receipt=result.is_receipt,
                invoice_date=result.invoice_date,
                product=result.product,
                company=result.company,
                price=result.price,
                currency=result.currency,
                vat=result.vat,
                confidence=result.confidence,
                source_used=result.source_used,
                raw_json=result.raw_json,
            )
        except Exception as exc:
            failures += 1
            print(f"[error] UID={outcome.uid} failed while saving: {exc}")
            if tracked_uid and outcome.uid_int is not None:
                uid_status[outcome.uid_int] = "error"
                advance_resume_checkpoint()
            return

        processed += 1
        if result.is_invoice or result.is_receipt:
            classified += 1

        print(
            f"[{processed}] UID={outcome.uid} "
            f"invoice={result.is_invoice} "
            f"receipt={result.is_receipt} "
            f"source={result.source_used}"
        )

        if tracked_uid and outcome.uid_int is not None:
            uid_status[outcome.uid_int] = "ok"
            advance_resume_checkpoint()

    try:
        with IMAPInbox(
            config.imap,
            password=imap_password,
            oauth_token=imap_oauth_token,
        ) as inbox, ThreadPoolExecutor(max_workers=workers) as pool:
            in_flight: dict[Future[MessageOutcome], None] = {}

            for uid, raw_email in inbox.fetch_messages():
                uid_int = _parse_uid_int(uid)

                if (
                    resume_enabled
                    and resume_last_uid is not None
                    and uid_int is not None
                    and uid_int <= resume_last_uid
                ):
                    skipped_resume += 1
                    continue

                if resume_enabled and uid_int is not None:
                    if uid_int not in uid_status:
                        ordered_uids.append(uid_int)
                    uid_status[uid_int] = "pending"

                future = pool.submit(
                    _process_message,
                    config=config,
                    uid=uid,
                    raw_email=raw_email,
                    openai_api_key=openai_api_key,
                )
                in_flight[future] = None
                submitted += 1

                if len(in_flight) >= max_in_flight:
                    done, _ = wait(
                        set(in_flight.keys()),
                        return_when=FIRST_COMPLETED,
                    )
                    for completed in done:
                        outcome = completed.result()
                        handle_outcome(outcome)
                        del in_flight[completed]

            while in_flight:
                done, _ = wait(
                    set(in_flight.keys()),
                    return_when=FIRST_COMPLETED,
                )
                for completed in done:
                    outcome = completed.result()
                    handle_outcome(outcome)
                    del in_flight[completed]
    finally:
        database.close()

    print(
        "Done. "
        f"submitted={submitted}, processed={processed}, "
        f"classified_as_invoice_or_receipt={classified}, "
        f"skipped_reply_or_answer={skipped_reply}, "
        f"skipped_non_invoice={skipped_non_invoice}, "
        f"failures={failures}, skipped_by_resume={skipped_resume}, "
        f"resume_last_uid={checkpoint_uid}, db={config.storage.database_path}"
    )
