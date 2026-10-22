"""Entry point for the Diwali B2B email campaign.

By default this performs a dry run.
Use --send to authorize live email submission.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

from config import settings
from src.data_handler import prepare_active_leads
from src.mailer import GmailMailer, CampaignMailerError 


def configure_logging() -> None:
    settings.ensure_directories()

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if root.handlers:
        root.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    file_handler = RotatingFileHandler(
        settings.LOG_FILE,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)


def count_successes_today(
    history_file: Path | None = None,
) -> int:
    """Count SMTP-accepted messages for the local calendar day."""
    history_path = history_file or settings.SEND_HISTORY_FILE

    if not history_path.exists():
        return 0

    today = datetime.now().astimezone().date()
    count = 0

    with history_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue

            try:
                record = json.loads(line)

                if record.get("status") != "smtp_accepted":
                    continue

                timestamp = datetime.fromisoformat(
                    str(record.get("timestamp", ""))
                ).astimezone()

                if timestamp.date() == today:
                    count += 1

            except (
                json.JSONDecodeError,
                ValueError,
                TypeError,
            ):
                logging.getLogger(__name__).warning(
                    "Ignoring malformed send-history entry "
                    "while counting daily sends"
                )

    return count


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and send personalized Diwali B2B email outreach"
        )
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=settings.RAW_LEADS_CSV,
        help="Source leads CSV",
    )
    parser.add_argument(
        "--active-output",
        type=Path,
        default=settings.ACTIVE_LEADS_CSV,
        help="Prepared active leads CSV",
    )
    parser.add_argument(
        "--rejected-output",
        type=Path,
        default=settings.REJECTED_LEADS_CSV,
        help="Rejected rows and reasons CSV",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help=(
            "Actually submit emails; without this flag, "
            "run a dry-run preview"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Maximum recipients to process in this run "
            "(cannot exceed the daily cap)"
        ),
    )

    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()

    configure_logging()
    logger = logging.getLogger("campaign")

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    dry_run = not args.send

    logger.info(
        "Starting Diwali outreach in %s mode",
        "DRY-RUN" if dry_run else "LIVE-SEND",
    )
    logger.info("Source CSV: %s", args.input.resolve())

    try:
        active_leads, stats = prepare_active_leads(
            input_path=args.input,
            active_output_path=args.active_output,
            rejected_output_path=args.rejected_output,
        )

    except (OSError, ValueError) as exc:
        logger.exception("Could not prepare leads: %s", exc)
        return 2

    logger.info("Preparation summary: %s", stats)
    logger.info(
        "Active leads saved to %s",
        args.active_output.resolve(),
    )
    logger.info(
        "Rejected rows saved to %s",
        args.rejected_output.resolve(),
    )

    if not active_leads:
        logger.warning("No eligible leads remain. Nothing to do.")
        return 0

    # Dry runs validate the data pipeline without sending or sleeping.
    if dry_run:
        preview_count = min(
            len(active_leads),
            args.limit or len(active_leads),
        )

        logger.info(
            "DRY-RUN preview: %d eligible email(s); "
            "nothing will be sent and no delay will be applied.",
            preview_count,
        )

        for lead in active_leads[:preview_count]:
            logger.info(
                "PREVIEW | to=%s | role=%s | company=%s | contact=%s",
                lead.get("Recipient_Email", ""),
                lead.get("Recipient_Role", ""),
                lead.get("Company_Name", ""),
                lead.get("Recipient_Name", ""),
            )

        logger.info(
            "For live sending, configure config/.env "
            "and rerun with --send."
        )
        return 0

    if not settings.CATALOG_URL:
        logger.error(
            "CATALOG_URL is empty. Add the Google Drive share "
            "link to config/.env before live sending."
        )
        return 2

    already_sent_today = count_successes_today()

    daily_remaining = max(
        0,
        settings.MAX_DAILY_SENDS - already_sent_today,
    )

    if daily_remaining == 0:
        logger.warning(
            "Daily limit reached: %d successful sends recorded "
            "today (limit=%d). Stopping safely.",
            already_sent_today,
            settings.MAX_DAILY_SENDS,
        )
        return 0

    run_limit = (
        args.limit
        if args.limit is not None
        else len(active_leads)
    )

    process_count = min(
        len(active_leads),
        run_limit,
        daily_remaining,
    )

    if process_count < len(active_leads):
        logger.info(
            "This run will process at most %d recipient(s); "
            "daily successful-send capacity remaining=%d.",
            process_count,
            daily_remaining,
        )

    successes = 0
    failures = 0

    try:
        with GmailMailer() as mailer:
            for index, lead in enumerate(
                active_leads[:process_count]
            ):
                recipient = lead.get("Recipient_Email", "")

                try:
                    accepted = mailer.send(lead)

                except CampaignMailerError as exc:
                    logger.critical(
                        "Fatal mailer error; campaign halted: %s",
                        exc,
                    )
                    return 3

                except (OSError, ValueError) as exc:
                    logger.exception(
                        "Could not submit to %s: %s",
                        recipient,
                        exc,
                    )
                    accepted = False

                if accepted:
                    successes += 1
                else:
                    failures += 1

                # Pause between attempts, including after a failed attempt.
                # There is no trailing delay after the final attempt.
                if index < process_count - 1:
                    delay = random.uniform(
                        settings.MIN_SEND_DELAY_SECONDS,
                        settings.MAX_SEND_DELAY_SECONDS,
                    )

                    logger.info(
                        "Waiting %.1f seconds before the next attempt.",
                        delay,
                    )
                    time.sleep(delay)

                # The limit counts successful SMTP handoffs, not attempts.
                if (
                    count_successes_today()
                    >= settings.MAX_DAILY_SENDS
                ):
                    logger.warning(
                        "Daily successful-send limit reached; "
                        "halting campaign safely."
                    )
                    break

    except KeyboardInterrupt:
        logger.warning(
            "Interrupted by user. SMTP connection closed; "
            "progress remains in send history."
        )
        return 130

    except (CampaignMailerError, OSError) as exc:
        logger.exception(
            "Campaign could not continue: %s",
            exc,
        )
        return 3

    logger.info(
        "Campaign run finished: SMTP-accepted=%d, "
        "failed_or_refused=%d, successful_today=%d/%d",
        successes,
        failures,
        count_successes_today(),
        settings.MAX_DAILY_SENDS,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())