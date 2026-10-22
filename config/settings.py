"""Central configuration for the Diwali B2B outreach project."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path(__file__).resolve().parent
ENV_FILE = CONFIG_DIR / ".env"
load_dotenv(dotenv_path=ENV_FILE, override=False)

DATA_DIR = PROJECT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
TEMPLATES_DIR = PROJECT_DIR / "templates"
OUTPUT_DIR = PROJECT_DIR / "output"
LOG_DIR = OUTPUT_DIR / "logs"

RAW_LEADS_CSV = RAW_DIR / "leads_master.csv"
ACTIVE_LEADS_CSV = PROCESSED_DIR / "leads_active.csv"
REJECTED_LEADS_CSV = PROCESSED_DIR / "leads_rejected.csv"
HTML_TEMPLATE_PATH = TEMPLATES_DIR / "email_body.html"
TEXT_TEMPLATE_PATH = TEMPLATES_DIR / "email_body.txt"
LOG_FILE = LOG_DIR / "campaign.log"
SEND_HISTORY_FILE = LOG_DIR / "send_history.jsonl"
SUPPRESSION_FILE = PROCESSED_DIR / "suppression_list.txt"

# OAuth desktop-client JSON is stored beside main.py by default.
def _project_path_from_env(name: str, default: Path) -> Path:
    configured = os.getenv(name, "").strip()
    if not configured:
        return default
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


OAUTH_CREDENTIALS_FILE = _project_path_from_env(
    "GOOGLE_OAUTH_CREDENTIALS_FILE", PROJECT_DIR / "credentials.json"
)
OAUTH_TOKEN_FILE = _project_path_from_env(
    "GOOGLE_OAUTH_TOKEN_FILE", PROJECT_DIR / "token.json"
)
GMAIL_API_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# The sender address can be omitted; then the authorized Gmail account is used.
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
SENDER_EMAIL = os.getenv("SENDER_EMAIL", GMAIL_ADDRESS).strip()
SENDER_NAME = os.getenv("SENDER_NAME", "WYS Gifting").strip()
REPLY_TO_EMAIL = os.getenv("REPLY_TO_EMAIL", SENDER_EMAIL).strip()

CATALOG_URL = os.getenv("CATALOG_URL", "").strip()
UNSUBSCRIBE_URL = os.getenv("UNSUBSCRIBE_URL", "").strip()
EMAIL_SUBJECT_TEMPLATE = os.getenv(
    "EMAIL_SUBJECT_TEMPLATE", "Diwali gifting ideas for {company_name}"
).strip()

# This ceiling is intentionally not configurable above 30.
_configured_daily_sends = int(os.getenv("MAX_DAILY_SENDS", "30"))
if _configured_daily_sends < 1:
    raise ValueError("MAX_DAILY_SENDS must be at least 1")
MAX_DAILY_SENDS = min(30, _configured_daily_sends)
MIN_SEND_DELAY_SECONDS = 60
MAX_SEND_DELAY_SECONDS = 69


def ensure_directories() -> None:
    """Create runtime directories without overwriting campaign data."""
    for directory in (RAW_DIR, PROCESSED_DIR, TEMPLATES_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)
