#!/usr/bin/env python3
"""Telegram channel -> Facebook Page cross-posting pipeline.

Fetches new channel posts via the Telegram Bot API, translates the text to
Bangla, overlays a logo onto any attached photo, and publishes the result to a
Facebook Page via the Meta Graph API. Processing state is persisted in
``state.json`` so old posts are never re-published.

Supports a DRY_RUN mode (env var or --dry-run) that prepares everything but
never posts to Facebook and never advances state, so the same post can be
re-tested repeatedly. A startup health check validates both API tokens.
"""

import argparse
import json
import logging
import os
import re
import shutil
import sys

import requests
from deep_translator import GoogleTranslator
from PIL import Image

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TELEGRAM_API = "https://api.telegram.org"
GRAPH_API_VERSION = "v21.0"
GRAPH_API = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

STATE_FILE = "state.json"
LOGO_FILE = "logo.png"
TEMP_IMAGE = "temp_post_image.jpg"
DRY_RUN_IMAGE = "dry_run_output.jpg"  # kept on disk for inspection in dry-run

LOGO_SCALE = 0.15  # logo width as a fraction of the base image width
LOGO_MARGIN = 15   # px margin from the bottom-right corner

REQUEST_TIMEOUT = 60  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tg2fb")


# --------------------------------------------------------------------------- #
# Environment / configuration helpers
# --------------------------------------------------------------------------- #

def is_dry_run():
    """Return True if DRY_RUN is enabled via the environment."""
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def translation_enabled():
    """Return True if posts should be translated to Bangla.

    Defaults to OFF -- posts go out as the original text (footer stripped). Set
    the TRANSLATE env var to 1/true to re-enable Google-Translate-to-Bangla.
    """
    return os.environ.get("TRANSLATE", "").strip().lower() in ("1", "true", "yes", "on")


def load_config():
    """Read required environment variables, failing loudly if any are missing."""
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID", "FACEBOOK_PAGE_TOKEN"]
    config = {}
    missing = []
    for name in required:
        value = os.environ.get(name)
        if not value:
            missing.append(name)
        config[name] = value

    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them before running app.py."
        )
    return config


# --------------------------------------------------------------------------- #
# Health checks
# --------------------------------------------------------------------------- #

def check_telegram_token(bot_token):
    """Validate the Telegram bot token via getMe. Returns True/False."""
    try:
        resp = requests.get(
            f"{TELEGRAM_API}/bot{bot_token}/getMe", timeout=REQUEST_TIMEOUT
        )
        data = resp.json()
        if resp.ok and data.get("ok"):
            bot = data.get("result", {})
            log.info(
                "HEALTH: Telegram token OK -> bot @%s (id=%s)",
                bot.get("username"), bot.get("id"),
            )
            return True
        log.error("HEALTH: Telegram token INVALID -> %s", data)
        return False
    except (requests.RequestException, ValueError) as exc:
        log.error("HEALTH: Telegram token check failed: %s", exc)
        return False


def check_facebook_token(page_token):
    """Validate the Facebook page token via /me. Soft failure (returns bool)."""
    try:
        resp = requests.get(
            f"{GRAPH_API}/me",
            params={"access_token": page_token, "fields": "id,name"},
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        if resp.ok and data.get("id"):
            log.info(
                "HEALTH: Facebook token OK -> %s (id=%s)",
                data.get("name"), data.get("id"),
            )
            return True
        log.warning("HEALTH: Facebook token soft-failed -> %s", data)
        return False
    except (requests.RequestException, ValueError) as exc:
        log.warning("HEALTH: Facebook token check soft-failed: %s", exc)
        return False


def run_health_check(config):
    """Run both token checks at startup. Returns (telegram_ok, facebook_ok)."""
    log.info("Running startup health check...")
    telegram_ok = check_telegram_token(config["TELEGRAM_BOT_TOKEN"])
    facebook_ok = check_facebook_token(config["FACEBOOK_PAGE_TOKEN"])
    if not telegram_ok:
        log.error("HEALTH: Telegram auth is broken; getUpdates will likely fail.")
    if not facebook_ok:
        log.warning("HEALTH: Facebook auth is a soft failure; continuing run.")
    return telegram_ok, facebook_ok


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #

def load_state():
    """Return the last processed update_id (0 if state is absent/unreadable)."""
    if not os.path.exists(STATE_FILE):
        log.info("No state file found; starting from last_update_id=0.")
        return 0
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        last = int(data.get("last_update_id", 0))
        log.info("Loaded state: last_update_id=%s", last)
        return last
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s (%s); defaulting to 0.", STATE_FILE, exc)
        return 0


def save_state(last_update_id):
    """Persist the newest processed update_id to disk."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"last_update_id": last_update_id}, fh)
        log.info("Saved state: last_update_id=%s", last_update_id)
    except OSError as exc:
        log.error("Failed to write %s: %s", STATE_FILE, exc)


# --------------------------------------------------------------------------- #
# Telegram helpers
# --------------------------------------------------------------------------- #

def get_updates(bot_token, offset):
    """Fetch channel updates from getUpdates using the given offset."""
    url = f"{TELEGRAM_API}/bot{bot_token}/getUpdates"
    params = {
        "offset": offset,
        "timeout": 10,
        "allowed_updates": json.dumps(["channel_post"]),
    }
    log.debug("Calling getUpdates with offset=%s", offset)
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("Failed to fetch Telegram updates: %s", exc)
        return []

    if not payload.get("ok"):
        log.error("Telegram getUpdates returned an error: %s", payload)
        return []

    return payload.get("result", [])


def download_photo(bot_token, photos):
    """Download the highest-resolution photo. Returns a local path or None."""
    if not photos:
        return None
    # Telegram sends an ascending list of sizes; the last is the largest.
    largest = max(photos, key=lambda p: p.get("file_size", p.get("width", 0)))
    file_id = largest.get("file_id")
    if not file_id:
        return None

    try:
        resp = requests.get(
            f"{TELEGRAM_API}/bot{bot_token}/getFile",
            params={"file_id": file_id},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        info = resp.json()
        if not info.get("ok"):
            log.error("getFile returned an error: %s", info)
            return None
        file_path = info["result"]["file_path"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.error("Failed to resolve Telegram file: %s", exc)
        return None

    download_url = f"{TELEGRAM_API}/file/bot{bot_token}/{file_path}"
    try:
        img_resp = requests.get(download_url, timeout=REQUEST_TIMEOUT)
        img_resp.raise_for_status()
        with open(TEMP_IMAGE, "wb") as fh:
            fh.write(img_resp.content)
        log.info("Downloaded photo to %s", TEMP_IMAGE)
        return TEMP_IMAGE
    except (requests.RequestException, OSError) as exc:
        log.error("Failed to download photo: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Text / image processing
# --------------------------------------------------------------------------- #

# A trailing "promotional footer" line: mostly decoration around an @handle,
# e.g. "📊@tech", "👉 @tech_news 👈", "@channel". Such lines are attribution
# noise that should not be posted to Facebook (and should not be translated).
_PROMO_LINE = re.compile(r"^[^\w@]*@\w+[^\w@]*$")


def clean_source_text(text):
    """Strip trailing promo/handle footer lines (e.g. '📊@tech') and whitespace.

    Only *trailing* lines that are essentially just an @mention (optionally
    wrapped in emoji/symbols) are removed, so real content that merely contains
    an @handle mid-sentence is left untouched.
    """
    if not text:
        return text
    lines = text.splitlines()
    while lines:
        last = lines[-1].strip()
        if last == "" or _PROMO_LINE.match(last):
            lines.pop()
        else:
            break
    return "\n".join(lines).strip()


def translate_text(text):
    """Translate text to Bangla, falling back to the original on failure."""
    if not text or not text.strip():
        return text
    try:
        translated = GoogleTranslator(source="auto", target="bn").translate(text)
        if translated:
            log.debug("Translated %d chars to Bangla.", len(text))
            return translated
        log.warning("Translation returned empty result; using original text.")
        return text
    except Exception as exc:  # deep-translator raises a variety of exceptions
        log.warning("Translation failed (%s); using original text.", exc)
        return text


def overlay_logo(image_path):
    """Overlay logo.png onto the bottom-right corner of the image."""
    if not os.path.exists(LOGO_FILE):
        log.warning("Logo file %s not found; skipping overlay.", LOGO_FILE)
        return image_path

    try:
        base = Image.open(image_path).convert("RGBA")
        logo = Image.open(LOGO_FILE).convert("RGBA")

        target_w = max(1, int(base.width * LOGO_SCALE))
        ratio = target_w / logo.width
        target_h = max(1, int(logo.height * ratio))
        logo = logo.resize((target_w, target_h), Image.LANCZOS)

        pos_x = base.width - target_w - LOGO_MARGIN
        pos_y = base.height - target_h - LOGO_MARGIN

        base.paste(logo, (pos_x, pos_y), mask=logo)

        # Flatten onto white so it can be saved as JPEG for Facebook.
        flattened = Image.new("RGB", base.size, (255, 255, 255))
        flattened.paste(base, mask=base.split()[3])
        flattened.save(image_path, "JPEG", quality=90)
        log.info("Overlaid logo onto %s", image_path)
        return image_path
    except (OSError, ValueError) as exc:
        log.error("Failed to overlay logo (%s); using original image.", exc)
        return image_path


# --------------------------------------------------------------------------- #
# Facebook publishing
# --------------------------------------------------------------------------- #

def resolve_page_id(page_token):
    """Resolve the Page id from the token; return 'me' as a safe fallback."""
    try:
        resp = requests.get(
            f"{GRAPH_API}/me",
            params={"access_token": page_token, "fields": "id,name"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        page_id = data.get("id")
        if page_id:
            log.info("Resolved Facebook page: %s (%s)", data.get("name"), page_id)
            return page_id
    except (requests.RequestException, ValueError) as exc:
        log.warning("Could not resolve page id (%s); falling back to 'me'.", exc)
    return "me"


def post_photo(page_id, page_token, image_path, caption):
    """Publish a photo with caption to the Facebook Page."""
    url = f"{GRAPH_API}/{page_id}/photos"
    try:
        with open(image_path, "rb") as fh:
            resp = requests.post(
                url,
                data={"caption": caption or "", "access_token": page_token},
                files={"source": fh},
                timeout=REQUEST_TIMEOUT,
            )
        _log_graph_response(resp)
        return resp.ok
    except (requests.RequestException, OSError) as exc:
        log.error("Failed to post photo to Facebook: %s", exc)
        return False


def post_text(page_id, page_token, message):
    """Publish a text-only post to the Facebook Page feed."""
    url = f"{GRAPH_API}/{page_id}/feed"
    try:
        resp = requests.post(
            url,
            data={"message": message or "", "access_token": page_token},
            timeout=REQUEST_TIMEOUT,
        )
        _log_graph_response(resp)
        return resp.ok
    except requests.RequestException as exc:
        log.error("Failed to post text to Facebook: %s", exc)
        return False


def _log_graph_response(resp):
    """Log the Graph API response body regardless of success/failure."""
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    if resp.ok:
        log.info("Graph API response (%s): %s", resp.status_code, body)
    else:
        log.error("Graph API error (%s): %s", resp.status_code, body)


# --------------------------------------------------------------------------- #
# Per-post processing
# --------------------------------------------------------------------------- #

def _save_dry_run_image(image_path):
    """Copy the processed image to a stable path for inspection. Returns path."""
    try:
        shutil.copyfile(image_path, DRY_RUN_IMAGE)
        return DRY_RUN_IMAGE
    except OSError as exc:
        log.warning("Could not save dry-run image copy: %s", exc)
        return image_path


def process_post(channel_post, config, page_id, dry_run):
    """Translate, decorate, and publish/simulate a single channel post.

    Returns one of: "posted", "dry_run", "skipped", "failed".
    """
    bot_token = config["TELEGRAM_BOT_TOKEN"]
    page_token = config["FACEBOOK_PAGE_TOKEN"]

    photos = channel_post.get("photo")
    raw_text = channel_post.get("caption") if photos else channel_post.get("text")
    text = clean_source_text(raw_text)
    if raw_text and text != raw_text:
        log.debug("Stripped promo/handle footer from source text.")

    # Translate to Bangla only when TRANSLATE is enabled; otherwise post the
    # original (footer-stripped) text as-is.
    if translation_enabled():
        body = translate_text(text)
    else:
        body = text

    if photos:
        image_path = download_photo(bot_token, photos)
        if image_path:
            image_path = overlay_logo(image_path)
            if dry_run:
                saved = _save_dry_run_image(image_path)
                log.info("[DRY-RUN] Would post PHOTO (logo applied). "
                         "Inspect image at: %s", saved)
                log.info("[DRY-RUN] Caption: %s", body)
                _cleanup(image_path)  # keep only the dry-run copy
                return "dry_run"
            success = post_photo(page_id, page_token, image_path, body)
            _cleanup(image_path)
            return "posted" if success else "failed"
        # If the download failed but we have a caption, still handle the text.
        if body and body.strip():
            if dry_run:
                log.info("[DRY-RUN] Photo download failed; would post caption "
                         "as TEXT: %s", body)
                return "dry_run"
            log.warning("Photo download failed; posting caption as text.")
            return "posted" if post_text(page_id, page_token, body) else "failed"
        log.warning("Photo download failed and no caption; skipping post.")
        return "failed"

    if body and body.strip():
        if dry_run:
            log.info("[DRY-RUN] Would post TEXT: %s", body)
            return "dry_run"
        return "posted" if post_text(page_id, page_token, body) else "failed"

    log.info("Post has no text or photo; nothing to publish.")
    return "skipped"


def _cleanup(path):
    """Remove a temporary file if it exists."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        log.warning("Could not remove temp file %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Telegram -> Facebook cross-posting pipeline."
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single processing pass (default behavior; explicit flag).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging so each step is shown clearly.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Prepare posts but do not publish to Facebook or advance state.",
    )
    parser.add_argument(
        "--health", action="store_true",
        help="Only run the token health check (getMe / Graph /me) and exit.",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv=None):
    args = parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        log.debug("Verbose logging enabled.")

    dry_run = args.dry_run or is_dry_run()

    log.info("Starting Telegram -> Facebook cross-posting run. dry_run=%s", dry_run)
    config = load_config()

    telegram_ok, _facebook_ok = run_health_check(config)

    # --health: report status and exit without processing anything.
    if args.health:
        log.info("Health check complete (--health); exiting.")
        return 0 if telegram_ok else 1

    last_update_id = load_state()
    # getUpdates offset = last processed id + 1 so we never re-fetch old posts.
    offset = last_update_id + 1 if last_update_id else 0

    updates = get_updates(config["TELEGRAM_BOT_TOKEN"], offset)
    fetched = len(updates)
    if not updates:
        log.info("No new updates to process.")
        log.info("SUMMARY: fetched=0 processed=0 posted=0 skipped=0 failed=0 "
                 "dry_run=%s", dry_run)
        return 0

    page_id = resolve_page_id(config["FACEBOOK_PAGE_TOKEN"])

    newest_update_id = last_update_id
    counts = {"posted": 0, "dry_run": 0, "skipped": 0, "failed": 0}
    processed = 0
    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id <= last_update_id:
            continue

        channel_post = update.get("channel_post")
        if channel_post:
            processed += 1
            try:
                result = process_post(channel_post, config, page_id, dry_run)
                counts[result] = counts.get(result, 0) + 1
                if result == "failed":
                    log.warning("Post for update_id=%s failed to publish.",
                                update_id)
            except Exception as exc:  # one bad post must not crash the run
                counts["failed"] += 1
                log.exception("Unexpected error processing update_id=%s: %s",
                              update_id, exc)
        else:
            log.info("Update %s has no channel_post; skipping.", update_id)

        # Advance the marker regardless so failed posts aren't retried forever.
        newest_update_id = max(newest_update_id, update_id)

    if dry_run:
        log.info("[DRY-RUN] State NOT advanced (would be %s); re-run freely.",
                 newest_update_id)
    elif newest_update_id > last_update_id:
        save_state(newest_update_id)

    log.info(
        "SUMMARY: fetched=%d processed=%d posted=%d skipped=%d failed=%d "
        "dry_run_simulated=%d dry_run=%s",
        fetched, processed, counts["posted"], counts["skipped"],
        counts["failed"], counts["dry_run"], dry_run,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - top-level safety net
        log.exception("Fatal error: %s", exc)
        sys.exit(1)
