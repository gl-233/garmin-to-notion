"""Sync Garmin sleep data to the Notion Sleep database."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from garminconnect import Garmin as GarminClient
from notion_client import Client as NotionClient

from garmin_to_notion.config import Settings
from garmin_to_notion.formatters import format_duration
from garmin_to_notion.notion_helpers import fetch_all_pages, get_prop

logger = logging.getLogger(__name__)


def _get_garmin_sleep_score(daily_sleep: dict) -> int | None:
    """Read Garmin's overall score without calculating a replacement."""
    scores = daily_sleep.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    value = overall.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 100 or int(value) != value:
        return None
    return int(value)


def _get_existing_sleep_dates(
    notion: NotionClient, database_id: str
) -> dict[str, dict]:
    """Fetch all existing sleep entries and return {date_str: page} mapping."""
    pages = fetch_all_pages(notion, database_id)
    result: dict[str, dict] = {}
    for page in pages:
        date_str = get_prop(page["properties"], "Date", "date")
        if date_str:
            result[date_str[:10]] = page
    return result


def _build_properties(sleep_data: dict, settings: Settings) -> dict | None:
    """Build Notion properties from Garmin sleep data. Returns None if no data."""
    daily_sleep = sleep_data.get("dailySleepDTO", {})
    if not daily_sleep:
        return None

    sleep_date = daily_sleep.get("calendarDate", "Unknown Date")
    total_sleep = sum(
        (daily_sleep.get(k, 0) or 0)
        for k in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds")
    )

    if total_sleep == 0:
        logger.info("Skipping sleep data for %s (total sleep is 0)", sleep_date)
        return None

    score = _get_garmin_sleep_score(daily_sleep)

    return {
        "Name": {
            "title": [{"text": {"content": format_duration(total_sleep)}}]
        },
        "Date": {"date": {"start": sleep_date}},
        "Duration": {
            "rich_text": [{"text": {"content": format_duration(total_sleep)}}]
        },
        "Deep": {
            "rich_text": [
                {
                    "text": {
                        "content": format_duration(
                            daily_sleep.get("deepSleepSeconds", 0) or 0
                        )
                    }
                }
            ]
        },
        "Light": {
            "rich_text": [
                {
                    "text": {
                        "content": format_duration(
                            daily_sleep.get("lightSleepSeconds", 0) or 0
                        )
                    }
                }
            ]
        },
        "REM": {
            "rich_text": [
                {
                    "text": {
                        "content": format_duration(
                            daily_sleep.get("remSleepSeconds", 0) or 0
                        )
                    }
                }
            ]
        },
        "Awake": {
            "rich_text": [
                {
                    "text": {
                        "content": format_duration(
                            daily_sleep.get("awakeSleepSeconds", 0) or 0
                        )
                    }
                }
            ]
        },
        "Resting HR": {"number": sleep_data.get("restingHeartRate", 0)},
        "Score": {"number": score},
    }


def sync_sleep(
    garmin: GarminClient,
    notion: NotionClient,
    settings: Settings,
) -> None:
    """Import missing nights and refresh Garmin scores within days_back.

    Existing dates are updated in place: no deletion or duplicate creation.
    Keep a large days_back for the first historical correction, then reduce
    it to 30 for the normal daily sync.
    """
    if not settings.sleep_db_id:
        logger.info("No sleep database configured, skipping")
        return

    existing_map = _get_existing_sleep_dates(notion, settings.sleep_db_id)
    today = datetime.now(tz=settings.timezone).date()
    created = updated = unchanged = skipped = 0

    for i in range(settings.days_back):
        date_str = (today - timedelta(days=i)).isoformat()
        # Pace the historical import rather than sending requests in a burst.
        if i:
            time.sleep(0.5)
        try:
            data = garmin.get_sleep_data(date_str)
        except Exception as exc:
            # Do not erase a score when Garmin cannot be reached.
            logger.error("Garmin sleep request failed for %s (%s); stopping",
                         date_str, type(exc).__name__)
            raise RuntimeError("Sleep sync interrupted; retry later") from None

        daily_sleep = (data or {}).get("dailySleepDTO") or {}
        if not daily_sleep or daily_sleep.get("calendarDate") != date_str:
            skipped += 1
            continue

        page = existing_map.get(date_str)
        if page:
            score = _get_garmin_sleep_score(daily_sleep)
            # An empty response must not erase an existing value.
            total_sleep = sum((daily_sleep.get(k) or 0) for k in
                              ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds"))
            if score is None and total_sleep <= 0:
                skipped += 1
                continue
            old_score = get_prop(page["properties"], "Score", "number")
            if old_score != score:
                notion.pages.update(page_id=page["id"],
                                    properties={"Score": {"number": score}})
                updated += 1
            else:
                unchanged += 1
        else:
            properties = _build_properties(data, settings)
            if properties:
                notion.pages.create(parent={"database_id": settings.sleep_db_id},
                                    properties=properties)
                created += 1
            else:
                skipped += 1

        if (i + 1) % 100 == 0:
            logger.info("Garmin sleep scores: %d/%d days checked, %d updated",
                        i + 1, settings.days_back, updated)

    logger.info("Sleep sync complete: %d created, %d Garmin scores updated, "
                "%d unchanged, %d skipped", created, updated, unchanged, skipped)
