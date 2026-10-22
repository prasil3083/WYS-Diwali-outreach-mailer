"""CSV loading, contact routing, duplicate removal, and suppression checks."""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)

REQUIRED_HEADERS = {
    "Company_Name",
    "HR_Email",
    "CEO_Email",
}

ROUTING_COLUMNS = [
    "Recipient_Name",
    "Recipient_Email",
    "Recipient_Role",
]


def clean(value: Any) -> str:
    """Normalize a CSV value into a trimmed, single-spaced string."""
    return " ".join(str(value or "").strip().split())


def canonical_company(value: str) -> str:
    """Normalize company names for duplicate checking."""
    return clean(value).casefold()


def normalize_email(value: Any) -> str:
    """Normalize an email address for comparison."""
    return clean(value).lower()


def valid_email(value: Any) -> bool:
    candidate = normalize_email(value)
    return bool(candidate and EMAIL_RE.fullmatch(candidate))


def load_suppression_list(
    path: Path | None = None,
) -> set[str]:
    """Read suppressed email addresses, one per line.

    Blank lines and lines beginning with # are ignored.
    """
    suppression_path = path or settings.SUPPRESSION_FILE

    if not suppression_path.exists():
        return set()

    suppressed: set[str] = set()

    with suppression_path.open(
        "r",
        encoding="utf-8-sig",
    ) as handle:
        for line in handle:
            value = line.strip().lower()

            if (
                value
                and not value.startswith("#")
                and valid_email(value)
            ):
                suppressed.add(value)

    return suppressed


def load_send_history(
    path: Path | None = None,
) -> tuple[set[str], set[str]]:
    """Return previously accepted addresses and company names."""
    history_path = path or settings.SEND_HISTORY_FILE

    sent_emails: set[str] = set()
    sent_companies: set[str] = set()

    if not history_path.exists():
        return sent_emails, sent_companies

    with history_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "Ignoring malformed send-history record at line %d",
                    line_number,
                )
                continue

            if record.get("status") != "smtp_accepted":
                continue

            email = normalize_email(record.get("email", ""))
            company = canonical_company(record.get("company", ""))

            if email:
                sent_emails.add(email)

            if company:
                sent_companies.add(company)

    return sent_emails, sent_companies


def _read_csv(
    input_path: Path,
) -> tuple[list[str], list[dict[str, str]]]:
    """Read the source CSV and validate its identifying columns."""
    if not input_path.is_file():
        raise FileNotFoundError(
            f"Input CSV was not found: {input_path}"
        )

    with input_path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        reader = csv.DictReader(handle)

        if not reader.fieldnames:
            raise ValueError(
                f"Input CSV has no header row: {input_path}"
            )

        headers = [clean(header) for header in reader.fieldnames]

        missing = sorted(REQUIRED_HEADERS.difference(headers))

        if missing:
            raise ValueError(
                "Input CSV is missing required columns: "
                + ", ".join(missing)
            )

        rows: list[dict[str, str]] = []

        for row in reader:
            normalized = {
                clean(key): clean(value)
                for key, value in row.items()
                if key is not None
            }

            rows.append({
                header: normalized.get(header, "")
                for header in headers
            })

    return headers, rows


def _select_contact(
    row: dict[str, str],
    suppressed: set[str],
    sent_emails: set[str],
) -> tuple[str, str, str] | None:
    """Prefer HR, falling back to the CEO when necessary."""
    candidates = (
        ("HR_Name", "HR_Email", "HR"),
        ("CEO_Name", "CEO_Email", "CEO"),
    )

    for name_key, email_key, role in candidates:
        email = normalize_email(row.get(email_key, ""))

        if not valid_email(email):
            continue

        if email in suppressed or email in sent_emails:
            continue

        default_name = "HR Team" if role == "HR" else "there"
        name = clean(row.get(name_key, "")) or default_name

        return name, email, role

    return None


def _write_csv(
    path: Path,
    headers: list[str],
    rows: list[dict[str, str]],
) -> None:
    """Write a CSV through a temporary file to avoid partial output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=headers,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

    temp_path.replace(path)


def prepare_active_leads(
    input_path: Path | None = None,
    active_output_path: Path | None = None,
    rejected_output_path: Path | None = None,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Prepare active leads and write rejected rows with explanations.

    Companies already contacted successfully are skipped on subsequent runs.
    If an HR address is invalid or suppressed, a usable CEO address may be used.
    """
    source = input_path or settings.RAW_LEADS_CSV
    active_path = active_output_path or settings.ACTIVE_LEADS_CSV
    rejected_path = rejected_output_path or settings.REJECTED_LEADS_CSV

    source_headers, source_rows = _read_csv(source)

    suppressed = load_suppression_list()
    sent_emails, sent_companies = load_send_history()

    active_headers = source_headers + [
        col for col in ROUTING_COLUMNS
        if col not in source_headers
    ]
    rejected_headers = source_headers + ["Reject_Reason"]

    active_rows: list[dict[str, str]] = []
    rejected_rows: list[dict[str, str]] = []

    seen_companies: set[str] = set()
    seen_emails: set[str] = set()

    for row_number, source_row in enumerate(source_rows, start=2):
        row = {
            key: clean(value)
            for key, value in source_row.items()
        }

        company = clean(row.get("Company_Name", ""))
        company_key = canonical_company(company)
        reason = ""

        if not company:
            reason = "Missing Company_Name"
        elif company_key in sent_companies:
            reason = (
                "Company already has an SMTP-accepted campaign email"
            )
        elif company_key in seen_companies:
            reason = "Duplicate company in input CSV"

        contact = (
            None
            if reason
            else _select_contact(row, suppressed, sent_emails)
        )

        if not reason and contact is None:
            candidate_emails = [
                normalize_email(row.get("HR_Email", "")),
                normalize_email(row.get("CEO_Email", "")),
            ]

            if any(
                email in suppressed
                for email in candidate_emails
                if email
            ):
                reason = (
                    "No usable contact; available address is invalid, "
                    "suppressed, or already sent"
                )
            else:
                reason = "No valid HR_Email or CEO_Email"

        if not reason and contact:
            recipient_name, recipient_email, recipient_role = contact

            if recipient_email in seen_emails:
                reason = "Duplicate recipient email in input CSV"
            else:
                row.update({
                    "Recipient_Name": recipient_name,
                    "Recipient_Email": recipient_email,
                    "Recipient_Role": recipient_role,
                })

                active_rows.append(row)
                seen_companies.add(company_key)
                seen_emails.add(recipient_email)

        if reason:
            rejected = dict(row)
            rejected["Reject_Reason"] = reason
            rejected_rows.append(rejected)

            logger.debug(
                "Rejected CSV row %d: %s",
                row_number,
                reason,
            )

    _write_csv(active_path, active_headers, active_rows)
    _write_csv(rejected_path, rejected_headers, rejected_rows)

    stats = {
        "input_rows": len(source_rows),
        "active_rows": len(active_rows),
        "rejected_rows": len(rejected_rows),
        "suppressed_addresses": len(suppressed),
        "previously_sent_companies": len(sent_companies),
    }

    logger.info(
        "Lead preparation complete: input=%d active=%d rejected=%d",
        stats["input_rows"],
        stats["active_rows"],
        stats["rejected_rows"],
    )

    return active_rows, stats