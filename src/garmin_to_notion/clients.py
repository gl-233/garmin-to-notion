"""Initialize Garmin and Notion, preserving the most recent valid session."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from garminconnect import Garmin as GarminClient
from notion_client import Client as NotionClient
from garmin_to_notion.config import Settings

logger = logging.getLogger(__name__)
TOKENSTORE_DIR = Path(os.getenv("GARMIN_TOKENSTORE", "~/.garmin_tokens")).expanduser()


@dataclass
class Clients:
    garmin: GarminClient
    notion: NotionClient


def _validate_bundle(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Invalid session bundle")
    if not all(isinstance(data.get(k), dict) for k in ("oauth1", "oauth2")):
        raise ValueError("Incomplete session bundle")
    for key in ("oauth_token", "oauth_token_secret"):
        if not data["oauth1"].get(key):
            raise ValueError("Incomplete OAuth1 session")
    if not data["oauth2"].get("access_token"):
        raise ValueError("Incomplete OAuth2 session")
    return data


def _load_tokens_from_env() -> dict | None:
    raw = os.getenv("GARMIN_TOKENS", "").strip()
    if not raw:
        return None
    try:
        return _validate_bundle(json.loads(base64.b64decode(raw)))
    except Exception:
        # Do not print secret contents or decoder exceptions.
        raise ValueError("GARMIN_TOKENS is invalid; replace the GitHub secret") from None


def _load_tokens_from_disk() -> dict | None:
    try:
        return _validate_bundle({
            "oauth1": json.loads((TOKENSTORE_DIR / "oauth1_token.json").read_text()),
            "oauth2": json.loads((TOKENSTORE_DIR / "oauth2_token.json").read_text()),
        })
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("Saved Garmin session is unreadable; using the configured secret")
        return None


def _expiry(tokens: dict) -> float:
    try:
        return float(tokens["oauth2"].get("expires_at", 0))
    except (ValueError, TypeError):
        return 0


def _select_tokens(secret: dict | None, cached: dict | None) -> tuple[dict | None, str]:
    if secret is None:
        return cached, "cache" if cached else "credentials"
    if cached is None:
        return secret, "GitHub secret"
    # A new browser login must take precedence over a cache from an old login.
    same_session = all(
        secret["oauth1"].get(key) == cached["oauth1"].get(key)
        for key in ("oauth_token", "oauth_token_secret")
    ) and (secret["oauth1"].get("domain") or "garmin.com") == (
        cached["oauth1"].get("domain") or "garmin.com"
    )
    if same_session and _expiry(cached) >= _expiry(secret):
        return cached, "cache"
    return secret, "GitHub secret"


def _load_profile(garmin: GarminClient) -> None:
    # Authentication is only successful after an actual authenticated request.
    logger.info("Checking Garmin user profile...")
    profile = garmin.garth.connectapi("/userprofile-service/socialProfile")
    if not isinstance(profile, dict) or not profile.get("displayName"):
        raise RuntimeError("Garmin did not return a valid user profile")
    garmin.display_name = profile["displayName"]
    garmin.full_name = profile.get("fullName") or garmin.display_name
    logger.info("Checking Garmin user settings...")
    user_settings = garmin.garth.connectapi("/userprofile-service/userprofile/user-settings")
    user_data = user_settings.get("userData", {}) if isinstance(user_settings, dict) else {}
    garmin.unit_system = user_data.get("measurementSystem") if isinstance(user_data, dict) else None


def _init_garmin_with_tokens(tokens: dict) -> GarminClient:
    import garth

    garmin = GarminClient()
    garmin.garth = garth.Client(domain=tokens["oauth1"].get("domain") or "garmin.com")
    garmin.garth.oauth1_token = garth.sso.OAuth1Token(**{
        k: v for k, v in tokens["oauth1"].items()
        if k in garth.sso.OAuth1Token.__dataclass_fields__
    })
    garmin.garth.oauth2_token = garth.sso.OAuth2Token(**{
        k: v for k, v in tokens["oauth2"].items()
        if k in garth.sso.OAuth2Token.__dataclass_fields__
    })
    _load_profile(garmin)
    return garmin


def save_garmin_tokens(garmin: GarminClient) -> None:
    """Save the live tokens, including renewals made during authenticated requests."""
    TOKENSTORE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    garmin.garth.dump(str(TOKENSTORE_DIR))
    for name in ("oauth1_token.json", "oauth2_token.json"):
        (TOKENSTORE_DIR / name).chmod(0o600)
    logger.info("Current Garmin session saved for the next run")


def init_clients(settings: Settings) -> Clients:
    logger.info("Checking Garmin connection...")
    try:
        tokens, source = _select_tokens(_load_tokens_from_env(), _load_tokens_from_disk())
        logger.info("Selected Garmin session source: %s", source)
        if tokens is not None:
            garmin = _init_garmin_with_tokens(tokens)
        else:
            garmin = GarminClient(settings.garmin_email, settings.garmin_password)
            garmin.login()
            _load_profile(garmin)
        save_garmin_tokens(garmin)
    except Exception as exc:
        status = _http_status(exc)
        if status == "429" or "TooManyRequests" in type(exc).__name__:
            logger.error("Garmin refused the connection (429). Stopping without further attempts.")
        else:
            logger.error("Garmin connection could not be verified (%s; HTTP %s). No sync started.", type(exc).__name__, status)
        raise SystemExit(1) from None
    logger.info("Garmin connection verified (session source: %s)", source)
    return Clients(garmin=garmin, notion=NotionClient(auth=settings.notion_token))


def _http_status(exc: Exception) -> str:
    """Extract only a status number; never log URLs, bodies or credentials."""
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 100 <= status <= 599:
            return str(status)
        match = re.search(r"\b([45]\d{2})\b", str(current))
        if match:
            return match.group(1)
        for attribute in ("error", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return "unknown"


def init_notion_only(settings: Settings) -> NotionClient:
    return NotionClient(auth=settings.notion_token)
