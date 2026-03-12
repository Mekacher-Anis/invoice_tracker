from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openai import OpenAI

AMOUNT_REGEX = re.compile(
    r"(?<!\w)(?:[$€£]\s*)?(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})|\d+[.,]\d{2})(?!\w)"
)
DATE_REGEXES = [
    re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b"),
]
VAT_REGEX = re.compile(r"\b(?:vat|tax)\D{0,12}(\d+(?:[.,]\d+)?)", flags=re.IGNORECASE)
INVOICE_HINTS = ("invoice", "tax invoice", "billing statement", "amount due")
RECEIPT_HINTS = ("receipt", "payment received", "order confirmation")


@dataclass
class ExtractionResult:
    is_invoice: bool
    is_receipt: bool
    invoice_date: str | None
    product: str | None
    company: str | None
    price: float | None
    currency: str | None
    vat: float | None
    confidence: float
    source_used: str
    raw_json: dict


def _to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace(",", ".")
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    for regex in DATE_REGEXES:
        match = regex.search(text)
        if not match:
            continue
        if regex is DATE_REGEXES[0]:
            year, month, day = match.groups()
        else:
            day, month, year = match.groups()
        try:
            normalized = datetime(int(year), int(month), int(day))
            return normalized.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _heuristic_extract(
    subject: str, sender: str, email_text: str, pdf_text: str
) -> ExtractionResult:
    combined = f"{subject}\n{sender}\n{pdf_text}\n{email_text}".lower()
    source_used = "pdf" if pdf_text.strip() else "email"

    is_invoice = any(hint in combined for hint in INVOICE_HINTS)
    is_receipt = any(hint in combined for hint in RECEIPT_HINTS)

    amount_match = AMOUNT_REGEX.search(combined)
    price = _to_float(amount_match.group(1) if amount_match else None)

    vat_match = VAT_REGEX.search(combined)
    vat = _to_float(vat_match.group(1) if vat_match else None)

    date_value: str | None = None
    for regex in DATE_REGEXES:
        match = regex.search(combined)
        if match:
            date_value = _normalize_date(match.group(0))
            break

    currency = None
    if "$" in combined:
        currency = "USD"
    elif "€" in combined or "eur" in combined:
        currency = "EUR"
    elif "£" in combined or "gbp" in combined:
        currency = "GBP"

    company = None
    if "@" in sender:
        domain = sender.split("@", 1)[1]
        domain = domain.split(">", 1)[0]
        domain = domain.split(".", 1)[0]
        company = domain.strip() or None

    product = subject.strip() or None

    return ExtractionResult(
        is_invoice=is_invoice,
        is_receipt=is_receipt,
        invoice_date=date_value,
        product=product,
        company=company,
        price=price,
        currency=currency,
        vat=vat,
        confidence=0.35,
        source_used=source_used,
        raw_json={"fallback": True},
    )


class InvoiceExtractor:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        model: str,
        temperature: float,
        max_pdf_pages_for_llm: int = 8,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._max_pdf_pages_for_llm = max(1, max_pdf_pages_for_llm)
        if api_key:
            client_kwargs = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            self._client = OpenAI(**client_kwargs)
        else:
            self._client = None

    @staticmethod
    def _image_to_data_url(path: Path) -> str:
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        suffix = path.suffix.lower()
        mime_type = "image/png"
        if suffix in {".jpg", ".jpeg"}:
            mime_type = "image/jpeg"
        elif suffix == ".webp":
            mime_type = "image/webp"
        return f"data:{mime_type};base64,{encoded}"

    def extract(
        self,
        *,
        subject: str,
        sender: str,
        email_text: str,
        pdf_text: str,
        pdf_page_images: list[Path] | None = None,
    ) -> ExtractionResult:
        page_images = (pdf_page_images or [])[: self._max_pdf_pages_for_llm]
        if self._client is None:
            return _heuristic_extract(subject, sender, email_text, pdf_text)

        source_used = "pdf_images" if page_images else "email"
        prompt_text = (
            "Classify the content and extract invoice/receipt fields.\n"
            "If page images are present, prioritize page images as the primary source.\n"
            "Use email text as secondary context.\n"
            "Return strict JSON with these keys only:\n"
            "is_invoice (bool), is_receipt (bool), date (YYYY-MM-DD or null),\n"
            "product (string or null), company (string or null), price (number or null),\n"
            "currency (string or null), vat (number or null), confidence (0..1).\n\n"
            f"Email subject:\n{subject}\n\n"
            f"Email sender:\n{sender}\n\n"
            f"Email body text:\n{email_text or '<none>'}\n"
        )

        content_parts: list[dict] = [{"type": "text", "text": prompt_text}]
        for image_path in page_images:
            try:
                data_url = self._image_to_data_url(image_path)
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    }
                )
            except Exception:
                continue

        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                temperature=self._temperature,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an invoice extraction engine. "
                            "Return only JSON, no markdown."
                        ),
                    },
                    {"role": "user", "content": content_parts},
                ],
                extra_body={
                    "chat_template_kwargs": {"enable_thinking": True},
                },
            )
            raw_content = completion.choices[0].message.content or "{}"
            payload = json.loads(raw_content)
        except Exception as exc:
            fallback = _heuristic_extract(subject, sender, email_text, pdf_text)
            fallback.raw_json = {"fallback": True, "error": str(exc)}
            return fallback

        confidence = _to_float(payload.get("confidence"))
        if confidence is None:
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        return ExtractionResult(
            is_invoice=bool(payload.get("is_invoice", False)),
            is_receipt=bool(payload.get("is_receipt", False)),
            invoice_date=_normalize_date(payload.get("date")),
            product=(
                str(payload.get("product")).strip()
                if payload.get("product") is not None
                else None
            ),
            company=(
                str(payload.get("company")).strip()
                if payload.get("company") is not None
                else None
            ),
            price=_to_float(payload.get("price")),
            currency=(
                str(payload.get("currency")).strip().upper()
                if payload.get("currency") is not None
                else None
            ),
            vat=_to_float(payload.get("vat")),
            confidence=confidence,
            source_used=source_used,
            raw_json=payload,
        )
