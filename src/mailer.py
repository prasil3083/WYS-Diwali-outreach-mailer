"""Gmail API mailer authenticated using a Google OAuth desktop client.

This module sends text/HTML alternatives with no attachments. It intentionally
uses Gmail API OAuth rather than SMTP passwords or forced SMTP reconnections.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
    r"|\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}"
    r"|\{([A-Za-z_][A-Za-z0-9_]*)\}"
    r"|\$([A-Za-z_][A-Za-z0-9_]*)"
)


class CampaignMailerError(RuntimeError):
    """Fatal authentication, permission, or campaign delivery error."""


class GmailMailer:
    """Gmail API sender with a compatible interface for the current main.py."""

    def __init__(self, history_file: Path | None = None) -> None:
        self.history_file = history_file or settings.SEND_HISTORY_FILE
        self._service = None
        self._sender_email = settings.SENDER_EMAIL or settings.GMAIL_ADDRESS
        self._text_template = self._read_template(settings.TEXT_TEMPLATE_PATH)
        self._html_template = self._read_template(settings.HTML_TEMPLATE_PATH)

    @staticmethod
    def _read_template(path: Path) -> str:
        if not path.is_file():
            raise FileNotFoundError(f"Email template was not found: {path}")
        return path.read_text(encoding="utf-8")

    def _authorize(self) -> None:
        """Load stored OAuth tokens or launch Google's browser consent flow."""
        if self._service is not None:
            return

        credentials_file = settings.OAUTH_CREDENTIALS_FILE
        token_file = settings.OAUTH_TOKEN_FILE

        if not credentials_file.is_file():
            raise CampaignMailerError(
                "OAuth client JSON was not found. Put your downloaded Desktop app "
                f"JSON at: {credentials_file}"
            )

        token_file.parent.mkdir(parents=True, exist_ok=True)
        credentials: Credentials | None = None

        if token_file.is_file():
            try:
                # Check the scopes recorded in the file itself before passing
                # requested scopes to Google's loader (which can override the
                # Credentials object's scope metadata).
                token_payload = json.loads(token_file.read_text(encoding="utf-8"))
                stored_scopes = set(token_payload.get("scopes", []))
                if set(settings.GMAIL_API_SCOPES).issubset(stored_scopes):
                    credentials = Credentials.from_authorized_user_file(
                        str(token_file), settings.GMAIL_API_SCOPES
                    )
                else:
                    logger.info(
                        "Stored OAuth token lacks Gmail send permission; "
                        "a new authorization will be requested."
                    )
            except (ValueError, json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load stored OAuth token: %s", exc)

        if credentials and not credentials.valid:
            if credentials.expired and credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                except RefreshError as exc:
                    logger.warning(
                        "Stored OAuth refresh token could not be refreshed; "
                        "starting authorization again: %s",
                        exc,
                    )
                    credentials = None
                except GoogleAuthError as exc:
                    raise CampaignMailerError(
                        f"Google OAuth token refresh failed: {exc}"
                    ) from exc
            else:
                credentials = None

        if not credentials or not credentials.valid:
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_file), settings.GMAIL_API_SCOPES
                )
                logger.info(
                    "Opening a browser for Gmail OAuth authorization. "
                    "Sign in to the Gmail account you intend to send from."
                )
                credentials = flow.run_local_server(port=0, open_browser=True)
            except Exception as exc:
                raise CampaignMailerError(
                    "Could not complete OAuth authorization. Confirm that "
                    "credentials.json is a Desktop app client JSON, the Gmail API "
                    "is enabled, and the account is allowed on the OAuth consent "
                    f"screen. Details: {exc}"
                ) from exc

        if not credentials or not credentials.valid:
            raise CampaignMailerError(
                "Google OAuth did not return valid credentials."
            )

        try:
            token_file.write_text(credentials.to_json(), encoding="utf-8")
            # Restrict token file permissions on platforms that support chmod.
            try:
                os.chmod(token_file, 0o600)
            except OSError:
                pass
        except OSError as exc:
            raise CampaignMailerError(
                f"Could not save OAuth token to {token_file}: {exc}"
            ) from exc

        # The gmail.send scope intentionally grants sending only. Do not call
        # users.getProfile here because that endpoint requires a broader scope.
        # The configured sender must be the OAuth account or a verified alias.
        if not self._sender_email:
            self._service = None
            raise CampaignMailerError(
                "Set SENDER_EMAIL or GMAIL_ADDRESS in config/.env to the Gmail "
                "account you authorized (or one of its verified send-as aliases)."
            )

        try:
            self._service = build(
                "gmail", "v1", credentials=credentials, cache_discovery=False
            )
        except Exception as exc:
            self._service = None
            raise CampaignMailerError(
                f"Could not initialize Gmail API client: {exc}"
            ) from exc

        logger.info(
            "Gmail API OAuth initialized; configured sender is %s",
            self._sender_email,
        )

    
    @staticmethod
    def _make_greeting(recipient_name: str) -> str:
        """Use a person's first name only when the value looks like a real name."""
        name = " ".join(str(recipient_name or "").strip().split())

        if not name:
            return "Hello,"

        # Avoid greeting departments, job titles, or placeholder values as people.
        invalid_values = {
            "there",
            "unknown",
            "n/a",
            "na",
            "none",
            "null",
            "hr",
            "hr department",
            "human resources",
            "human resources department",
            "ceo",
            "ceo department",
            "department",
            "hr team",
            "management",
            "management team",
            "recruitment",
            "recruitment department",
            "admin",
            "admin department",
            "contact person",
            "recipient name",
        }

        if name.casefold() in invalid_values:
            return "Hello,"

        if any(char in name for char in "@{}<>$"):
            return "Hello,"

        if any(
            word in name.casefold().split()
            for word in (
                "department",
                "team",
                "division",
                "office",
                "administrator",
                "manager",
                "director",
                "executive",
                "human",
                "resources",
            )
        ):
            return "Hello,"

        # Use the first word as the person's first name.
        first_name = name.split()[0].strip(".,;:")
        if not first_name or not first_name.isalpha():
            return "Hello,"

        return f"Hi {first_name},"


    @staticmethod
    def _context(lead: dict[str, Any], sender_email: str) -> dict[str, str]:
        company_name = str(lead.get("Company_Name", "") or "there").strip() or "there"
        recipient_name = str(lead.get("Recipient_Name", "") or "there").strip() or "there"

        values = {
            "company_name": company_name,
            "contact_name": recipient_name,
            "recipient_name": recipient_name,
            "recipient_email": str(lead.get("Recipient_Email", "") or "").strip(),
            "recipient_role": str(lead.get("Recipient_Role", "") or "").strip(),
            "industry": str(lead.get("Industry_Category", "") or "").strip(),
            "employee_strength_band": str(lead.get("Employee_Strength_Band", "") or "").strip(),
            "employee_count": str(lead.get("Estimated_Employee_Count", "") or "").strip(),
            "location": str(lead.get("Gujarat_Location", "") or "").strip(),
            "website": str(lead.get("Official_Website", "") or "").strip(),
            "sender_name": settings.SENDER_NAME,
            "sender_email": sender_email,
            "catalog_url": settings.CATALOG_URL,
            "reply_to_email": settings.REPLY_TO_EMAIL or sender_email,
            "unsubscribe_url": settings.UNSUBSCRIBE_URL,
        }
                # Also permit any original CSV key in templates.
        for key, value in lead.items():
            if key:
                values[str(key)] = str(value or "").strip()

        # Set the greeting after CSV fields have been copied into the context.
        # This ensures the greeting follows the validation rules above.
        values["greeting"] = GmailMailer._make_greeting(
            str(lead.get("Recipient_Name", "") or "")
        )

        return values

    @staticmethod
    def _render(
        template: str, context: dict[str, str], *, html_mode: bool = False
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            key = next((group for group in match.groups() if group is not None), "")
            value = context.get(key, "")
            return html.escape(value, quote=True) if html_mode else value

        return _TOKEN_RE.sub(replace, template)

    def build_message(self, lead: dict[str, Any]) -> EmailMessage:
        recipient = str(lead.get("Recipient_Email", "") or "").strip()
        if not recipient:
            raise ValueError("Lead is missing Recipient_Email")

        sender_email = self._sender_email or settings.SENDER_EMAIL or settings.GMAIL_ADDRESS
        if not sender_email:
            raise CampaignMailerError(
                "Could not determine the sender address. Complete OAuth authorization first."
            )

        context = self._context(lead, sender_email)
        try:
            subject = settings.EMAIL_SUBJECT_TEMPLATE.format_map(_MissingFormatKey(context))
        except (ValueError, TypeError) as exc:
            raise CampaignMailerError(f"Invalid EMAIL_SUBJECT_TEMPLATE: {exc}") from exc
        subject = " ".join(subject.split()).strip()
        if not subject:
            raise CampaignMailerError("Email subject rendered as blank")

        text_body = self._render(self._text_template, context)
        html_body = self._render(self._html_template, context, html_mode=True)
        text_body, html_body = self._append_opt_out(text_body, html_body)

        message = EmailMessage()
        message["From"] = formataddr((settings.SENDER_NAME, sender_email))
        message["To"] = recipient
        message["Subject"] = subject
        reply_to = settings.REPLY_TO_EMAIL or sender_email
        if reply_to:
            message["Reply-To"] = reply_to
        if settings.UNSUBSCRIBE_URL:
            message["List-Unsubscribe"] = f"<{settings.UNSUBSCRIBE_URL}>"

        # multipart/alternative only; no files are attached.
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
        return message

    @staticmethod
    def _append_opt_out(text_body: str, html_body: str) -> tuple[str, str]:
        if "unsubscribe" in text_body.casefold() or "opt out" in text_body.casefold():
            return text_body, html_body

        if settings.UNSUBSCRIBE_URL:
            link = settings.UNSUBSCRIBE_URL
            text_footer = f"\n\nTo stop receiving these emails, unsubscribe here: {link}\n"
            html_footer = (
                '<p style="font-size:12px;color:#666">To stop receiving these emails, '
                f'<a href="{html.escape(link, quote=True)}">unsubscribe here</a>.</p>'
            )
        else:
            text_footer = (
                '\n\nIf you would prefer not to receive further emails, '
                'reply with "unsubscribe" and we will suppress this address.\n'
            )
            html_footer = (
                '<p style="font-size:12px;color:#666">If you would prefer not to '
                'receive further emails, reply with "unsubscribe" and we will '
                'suppress this address.</p>'
            )
        return text_body.rstrip() + text_footer, html_body.rstrip() + html_footer

    def connect(self) -> None:
        """Compatibility method: authorize and initialize the Gmail API client."""
        self._authorize()

    def send(self, lead: dict[str, Any]) -> bool:
        """Send one message; ambiguous transport failures are not retried."""
        self.connect()
        message = self.build_message(lead)
        recipient = str(lead.get("Recipient_Email", "") or "").strip().lower()
        company = str(lead.get("Company_Name", "") or "").strip()

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        try:
            assert self._service is not None
            response = (
                self._service.users()
                .messages()
                .send(userId="me", body={"raw": raw_message})
                .execute()
            )
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (401, 403):
                raise CampaignMailerError(
                    "Gmail API authorization/permission error. Reauthorize OAuth, "
                    "verify the gmail.send scope, and check the OAuth consent screen. "
                    f"Details: {exc}"
                ) from exc
            logger.error(
                "Gmail API rejected message for %s (HTTP %s): %s",
                recipient,
                status,
                exc,
            )
            return False
        except (OSError, TimeoutError) as exc:
            logger.error(
                "Gmail API transport failed for %s. It will not be retried automatically "
                "because acceptance may be ambiguous: %s",
                recipient,
                exc,
            )
            return False

        message_id = str(response.get("id", ""))
        try:
            self._record_success(recipient, company, message_id)
        except OSError as exc:
            # Avoid continuing: the API accepted the send but local dedup history failed.
            raise CampaignMailerError(
                "Gmail accepted the message, but send history could not be saved. "
                "Check whether the email arrived before restarting the campaign. "
                f"History error: {exc}"
            ) from exc

        logger.info(
            "Gmail API accepted email for %s at %s (message_id=%s)",
            recipient,
            company or "(company not provided)",
            message_id or "not returned",
        )
        return True

    def _record_success(self, recipient: str, company: str, message_id: str) -> None:
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "email": recipient,
            "company": company,
            "status": "smtp_accepted",  # Kept for compatibility with existing history readers.
            "transport": "gmail_api",
            "gmail_message_id": message_id,
        }
        with self.history_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def close(self) -> None:
        """Release the local API client reference; Google handles HTTP connections."""
        self._service = None

    def __enter__(self) -> "GmailMailer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class _MissingFormatKey(dict[str, str]):
    """Resolve missing subject placeholders to an empty string."""

    def __missing__(self, key: str) -> str:
        return ""
