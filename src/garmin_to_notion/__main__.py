"""Command-line entry point for Garmin to Notion."""

from __future__ import annotations

import argparse
import logging

from garmin_to_notion.config import load_settings
from garmin_to_notion.log import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Garmin fitness data to Notion databases")
    parser.add_argument("command", nargs="?", default="all", choices=[
        "all", "activities", "records", "steps", "sleep", "workouts", "summary", "cleanup",
    ])
    parser.add_argument("--execute", action="store_true", help="Archive duplicates during cleanup")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    logger = logging.getLogger(__name__)
    settings = load_settings(require_garmin=args.command not in ("cleanup", "summary"))

    if not settings.has_all_db_ids:
        from notion_client import Client as NotionClient
        from garmin_to_notion.notion_helpers import discover_databases

        logger.info("Discovering missing Notion databases...")
        settings = settings.with_discovered_ids(discover_databases(NotionClient(auth=settings.notion_token)))

    if args.command == "cleanup":
        from garmin_to_notion.clients import init_notion_only
        from garmin_to_notion.tools.cleanup_duplicates import cleanup_duplicates

        cleanup_duplicates(init_notion_only(settings), settings, dry_run=not args.execute)
        return

    if args.command == "summary":
        from garmin_to_notion.clients import init_notion_only
        from garmin_to_notion.syncers.summary import sync_summary
        if not settings.summary_db_id:
            logger.error("Missing Notion database for %s", args.command)
            raise SystemExit(1)
        sync_summary(init_notion_only(settings), settings)
        return

    from garmin_to_notion.clients import init_clients, save_garmin_tokens
    from garmin_to_notion.syncers.activities import sync_activities
    from garmin_to_notion.syncers.daily_steps import sync_daily_steps
    from garmin_to_notion.syncers.personal_records import sync_personal_records
    from garmin_to_notion.syncers.sleep import sync_sleep
    from garmin_to_notion.syncers.summary import sync_summary
    from garmin_to_notion.syncers.workouts import sync_workouts

    clients = init_clients(settings)
    sync_map = {
        "activities": lambda: sync_activities(clients.garmin, clients.notion, settings),
        "records": lambda: sync_personal_records(clients.garmin, clients.notion, settings),
        "steps": lambda: sync_daily_steps(clients.garmin, clients.notion, settings),
        "sleep": lambda: sync_sleep(clients.garmin, clients.notion, settings),
        "workouts": lambda: sync_workouts(clients.notion, settings),
        "summary": lambda: sync_summary(clients.notion, settings),
    }
    db_check = {
        "activities": settings.activities_db_id, "records": settings.pr_db_id,
        "steps": settings.steps_db_id, "sleep": settings.sleep_db_id,
        "workouts": settings.workouts_db_id, "summary": settings.summary_db_id,
    }
    commands = list(sync_map) if args.command == "all" else [args.command]
    failed = False
    try:
        for cmd in commands:
            if not db_check.get(cmd):
                if args.command == "all":
                    logger.info("Skipping %s (no database configured)", cmd)
                    continue
                logger.error("Missing Notion database for %s", cmd)
                failed = True
                break
            try:
                logger.info("Starting %s sync...", cmd)
                sync_map[cmd]()
            except Exception as exc:
                logger.error("%s sync failed (%s). Stopping; later syncs were not run.", cmd, type(exc).__name__)
                failed = True
                break
    finally:
        try:
            save_garmin_tokens(clients.garmin)
        except Exception as exc:
            logger.error("Could not save the renewed Garmin session (%s)", type(exc).__name__)
            failed = True
    if failed:
        raise SystemExit(1)
    logger.info("All requested, configured syncs completed without a reported error")


if __name__ == "__main__":
    main()
