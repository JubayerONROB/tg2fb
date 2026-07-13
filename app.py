#!/usr/bin/env python3
"""Telegram channel -> Facebook Page cross-posting pipeline.

Fetches new channel posts via the Telegram Bot API, translates the text to
Bangla, overlays a logo onto any attached photo, and publishes the result to a
Facebook Page via the Meta Graph API. Processing state is persisted in
``state.json`` so old posts are never re-published.
"""

import json
import logging
import os
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

def translate_text(text):
    """Translate text to Bangla, falling back to the original on failure."""
    if not text or not text.strip():
        return text
    try:
        translated = GoogleTranslator(source="auto", target="bn").translate(text)
        if translated:
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

def process_post(channel_post, config, page_id):
    """Translate, decorate, and publish a single channel post."""
    bot_token = config["TELEGRAM_BOT_TOKEN"]
    page_token = config["FACEBOOK_PAGE_TOKEN"]

    photos = channel_post.get("photo")
    text = channel_post.get("caption") if photos else channel_post.get("text")
    translated = translate_text(text)

    if photos:
        image_path = download_photo(bot_token, photos)
        if image_path:
            image_path = overlay_logo(image_path)
            success = post_photo(page_id, page_token, image_path, translated)
            _cleanup(image_path)
            return success
        # If the download failed but we have a caption, still post the text.
        if translated:
            log.warning("Photo download failed; posting caption as text.")
            return post_text(page_id, page_token, translated)
        log.warning("Photo download failed and no caption; skipping post.")
        return False

    if translated and translated.strip():
        return post_text(page_id, page_token, translated)

    log.info("Post has no text or photo; nothing to publish.")
    return True  # nothing to do, but not a failure


def _cleanup(path):
    """Remove a temporary file if it exists."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        log.warning("Could not remove temp file %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    log.info("Starting Telegram -> Facebook cross-posting run.")
    config = load_config()

    last_update_id = load_state()
    # getUpdates offset = last processed id + 1 so we never re-fetch old posts.
    offset = last_update_id + 1 if last_update_id else 0

    updates = get_updates(config["TELEGRAM_BOT_TOKEN"], offset)
    if not updates:
        log.info("No new updates to process.")
        return

    page_id = resolve_page_id(config["FACEBOOK_PAGE_TOKEN"])

    newest_update_id = last_update_id
    processed = 0
    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id <= last_update_id:
            continue

        channel_post = update.get("channel_post")
        if channel_post:
            try:
                if process_post(channel_post, config, page_id):
                    processed += 1
                else:
                    log.warning("Post for update_id=%s failed to publish.", update_id)
            except Exception as exc:  # one bad post must not crash the run
                log.exception("Unexpected error processing update_id=%s: %s",
                              update_id, exc)
        else:
            log.info("Update %s has no channel_post; skipping.", update_id)

        # Advance the marker regardless so failed posts aren't retried forever.
        newest_update_id = max(newest_update_id, update_id)

    if newest_update_id > last_update_id:
        save_state(newest_update_id)

    log.info("Run complete. Published %s post(s).", processed)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - top-level safety net
        log.exception("Fatal error: %s", exc)
        sys.exit(1)
