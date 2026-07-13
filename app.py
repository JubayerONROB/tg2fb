#!/usr/bin/env python3
"""Telegram channel -> Facebook Page cross-posting pipeline.

Fetches new channel posts via the Telegram Bot API and enqueues them into a
persistent FIFO queue (``queue.json``), decoupled from the Telegram fetch
offset (``state.json``). Each run posts a small, rate-limited number of items
(default one) to a Facebook Page via the Meta Graph API, so a burst of channel
posts is spread out over hours instead of flooding the Page.

Supported media: text, a single photo, a photo album (Telegram media groups),
and video (watermarked with ffmpeg). Photos get a logo overlay; videos get a
persistent bottom-right watermark.

A DRY_RUN mode prepares everything but never posts and never mutates state or
the queue, so items can be re-tested repeatedly. A startup health check
validates both API tokens.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
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
QUEUE_FILE = "queue.json"
DEAD_LETTER_FILE = "queue_dead_letter.json"
LOGO_FILE = "logo.png"

# Working / inspection files (all git-ignored).
TEMP_MEDIA_PREFIX = "temp_media_"      # temp_media_0.jpg, temp_media_1.jpg, ...
TEMP_VIDEO = "temp_video.mp4"
WATERMARKED_VIDEO = "temp_video_wm.mp4"
DRY_RUN_IMAGE = "dry_run_output.jpg"   # single photo / first album image
DRY_RUN_VIDEO = "dry_run_output.mp4"

LOGO_SCALE = 0.15  # logo width as a fraction of the base image/video width
LOGO_MARGIN = 15   # px margin from the bottom-right corner

REQUEST_TIMEOUT = 60        # seconds for normal API calls
VIDEO_UPLOAD_TIMEOUT = 300  # seconds for the (slower) video upload

# Telegram Bot API can only download files up to 20 MB via getFile.
VIDEO_MAX_BYTES = 20 * 1024 * 1024

MAX_RETRIES = 3  # after this many failed attempts, an item is dead-lettered

# Emoji the bot reacts with on the source Telegram message after a successful
# Facebook post. Must be one of the reactions the channel allows.
REACTION_EMOJI = "💯"

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


def posts_per_run():
    """How many queue items to post per run (default 1). Set via POSTS_PER_RUN."""
    raw = os.environ.get("POSTS_PER_RUN", "1").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("Invalid POSTS_PER_RUN=%r; defaulting to 1.", raw)
        return 1


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
# JSON persistence (state / queue / dead-letter)
# --------------------------------------------------------------------------- #

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s (%s); using default.", path, exc)
        return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        return True
    except OSError as exc:
        log.error("Failed to write %s: %s", path, exc)
        return False


def load_state():
    """Return the last processed update_id (0 if state is absent/unreadable)."""
    data = _load_json(STATE_FILE, {})
    try:
        last = int(data.get("last_update_id", 0))
    except (ValueError, TypeError):
        last = 0
    log.info("Loaded state: last_update_id=%s", last)
    return last


def save_state(last_update_id):
    """Persist the newest processed update_id to disk."""
    if _save_json(STATE_FILE, {"last_update_id": last_update_id}):
        log.info("Saved state: last_update_id=%s", last_update_id)


def load_queue():
    """Return the pending-items queue (a list)."""
    data = _load_json(QUEUE_FILE, [])
    return data if isinstance(data, list) else []


def save_queue(queue):
    """Persist the pending-items queue."""
    _save_json(QUEUE_FILE, queue)


def dead_letter(item):
    """Append a permanently-failed item to the dead-letter queue."""
    dl = _load_json(DEAD_LETTER_FILE, [])
    if not isinstance(dl, list):
        dl = []
    dl.append(item)
    _save_json(DEAD_LETTER_FILE, dl)
    log.warning("Moved item to dead-letter queue (type=%s, update_ids=%s).",
                item.get("type"), item.get("update_ids"))


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


def react_to_messages(bot_token, chat_id, message_ids, emoji=REACTION_EMOJI):
    """React with an emoji to the source Telegram message(s) via setMessageReaction.

    Best-effort: a failed reaction is logged but never affects the post outcome.
    Returns the number of messages reacted to.
    """
    if not chat_id or not message_ids:
        log.debug("No chat_id/message_ids available; skipping reaction.")
        return 0
    url = f"{TELEGRAM_API}/bot{bot_token}/setMessageReaction"
    reaction = json.dumps([{"type": "emoji", "emoji": emoji}])
    reacted = 0
    for message_id in message_ids:
        if message_id is None:
            continue
        try:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "message_id": message_id,
                      "reaction": reaction},
                timeout=REQUEST_TIMEOUT,
            )
            data = resp.json()
            if resp.ok and data.get("ok"):
                reacted += 1
                log.info("Reacted %s to Telegram message %s.", emoji, message_id)
            else:
                log.warning("setMessageReaction failed for message %s: %s",
                            message_id, data)
        except (requests.RequestException, ValueError) as exc:
            log.warning("Reaction request failed for message %s: %s",
                        message_id, exc)
    return reacted


def resolve_file(bot_token, file_id):
    """Resolve a Telegram file path via getFile.

    Returns (file_path or None, too_big: bool). ``too_big`` flags the Bot API's
    "file is too big" error so callers can dead-letter instead of retrying.
    """
    try:
        resp = requests.get(
            f"{TELEGRAM_API}/bot{bot_token}/getFile",
            params={"file_id": file_id},
            timeout=REQUEST_TIMEOUT,
        )
        info = resp.json()
        if not info.get("ok"):
            desc = str(info.get("description", "")).lower()
            too_big = "too big" in desc
            log.error("getFile returned an error: %s", info)
            return None, too_big
        return info["result"]["file_path"], False
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.error("Failed to resolve Telegram file: %s", exc)
        return None, False


def download_file(bot_token, file_id, dest_path):
    """Download a Telegram file to dest_path. Returns (path or None, too_big)."""
    file_path, too_big = resolve_file(bot_token, file_id)
    if not file_path:
        return None, too_big
    download_url = f"{TELEGRAM_API}/file/bot{bot_token}/{file_path}"
    try:
        resp = requests.get(download_url, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)
        log.info("Downloaded Telegram file to %s", dest_path)
        return dest_path, False
    except (requests.RequestException, OSError) as exc:
        log.error("Failed to download Telegram file: %s", exc)
        return None, False


# --------------------------------------------------------------------------- #
# Queue building (Telegram updates -> queue items)
# --------------------------------------------------------------------------- #

def _largest_photo_id(photos):
    """Return the file_id of the highest-resolution photo size."""
    largest = max(photos, key=lambda p: p.get("file_size", p.get("width", 0)))
    return largest.get("file_id")


def _item_from_update(update):
    """Convert a single update into a queue item, or None if unsupported."""
    cp = update.get("channel_post")
    if not cp:
        return None
    update_id = update.get("update_id")
    mgid = cp.get("media_group_id")
    chat_id = (cp.get("chat") or {}).get("id")
    message_id = cp.get("message_id")

    if cp.get("photo"):
        return {
            "type": "photo",
            "photo_file_ids": [_largest_photo_id(cp["photo"])],
            "caption": cp.get("caption") or "",
            "entities": cp.get("caption_entities") or [],
            "media_group_id": mgid,
            "chat_id": chat_id,
            "message_ids": [message_id],
            "update_ids": [update_id],
            "retry_count": 0,
        }
    if cp.get("video"):
        video = cp["video"]
        return {
            "type": "video",
            "video_file_id": video.get("file_id"),
            "video_file_size": video.get("file_size"),
            "caption": cp.get("caption") or "",
            "entities": cp.get("caption_entities") or [],
            "media_group_id": mgid,
            "chat_id": chat_id,
            "message_ids": [message_id],
            "update_ids": [update_id],
            "retry_count": 0,
        }
    if cp.get("text"):
        return {
            "type": "text",
            "caption": cp.get("text"),
            "entities": cp.get("entities") or [],
            "media_group_id": None,
            "chat_id": chat_id,
            "message_ids": [message_id],
            "update_ids": [update_id],
            "retry_count": 0,
        }
    return None


def build_items(updates):
    """Group updates into queue items, merging same-batch photo albums.

    Photos sharing a media_group_id that arrive together are merged into one
    album item with multiple file_ids. Only stable Telegram file_ids are stored
    (never getFile URLs, which expire).
    """
    items = []
    group_index = {}  # media_group_id -> index in items (photo albums only)
    for update in sorted(updates, key=lambda u: u.get("update_id", 0)):
        item = _item_from_update(update)
        if item is None:
            uid = update.get("update_id")
            if update.get("channel_post"):
                log.info("Update %s has unsupported media; skipping.", uid)
            continue

        mgid = item.get("media_group_id")
        if item["type"] == "photo" and mgid and mgid in group_index:
            existing = items[group_index[mgid]]
            existing["photo_file_ids"].extend(item["photo_file_ids"])
            existing["update_ids"].extend(item["update_ids"])
            existing["message_ids"].extend(item["message_ids"])
            if not existing["caption"] and item["caption"]:
                existing["caption"] = item["caption"]
                existing["entities"] = item.get("entities") or []
            existing["type"] = (
                "album" if len(existing["photo_file_ids"]) > 1 else "photo"
            )
        else:
            items.append(item)
            if item["type"] == "photo" and mgid:
                group_index[mgid] = len(items) - 1
    return items


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


# Unicode "Mathematical Sans-Serif Bold" ranges -- the closest thing to real
# bold that survives in plain text (Facebook's Graph API has no rich text).
_BOLD_UPPER = 0x1D5D4  # 𝗔 (maps A-Z contiguously)
_BOLD_LOWER = 0x1D5EE  # 𝗮 (maps a-z contiguously)
_BOLD_DIGIT = 0x1D7EC  # 𝟬 (maps 0-9 contiguously)


def to_unicode_bold(text):
    """Map ASCII letters/digits to Unicode bold lookalikes; leave all else as-is.

    Characters without a bold equivalent -- spaces, punctuation, emoji, and any
    non-Latin script such as Bangla -- pass through completely unchanged.
    """
    out = []
    for ch in text:
        o = ord(ch)
        if 0x41 <= o <= 0x5A:      # A-Z
            out.append(chr(o - 0x41 + _BOLD_UPPER))
        elif 0x61 <= o <= 0x7A:    # a-z
            out.append(chr(o - 0x61 + _BOLD_LOWER))
        elif 0x30 <= o <= 0x39:    # 0-9
            out.append(chr(o - 0x30 + _BOLD_DIGIT))
        else:
            out.append(ch)
    return "".join(out)


def apply_bold_entities(text, entities):
    """Bold the substrings that Telegram marked as `bold`, leaving the rest.

    Telegram entity `offset`/`length` are measured in UTF-16 code units, not
    Python characters, so an emoji (a surrogate pair) counts as 2. We slice the
    UTF-16-LE byte buffer at code-unit boundaries -- which Telegram guarantees
    never split a surrogate pair -- so bold ranges stay aligned regardless of
    emoji or multi-byte content. Non-bold entity types (text_link, etc.) are
    ignored.
    """
    if not text or not entities:
        return text
    spans = sorted(
        ((e["offset"], e["length"]) for e in entities
         if e.get("type") == "bold" and "offset" in e and "length" in e),
        key=lambda s: s[0],
    )
    if not spans:
        return text

    data = text.encode("utf-16-le")  # 2 bytes per UTF-16 code unit
    total_units = len(data) // 2

    # Merge overlapping/adjacent spans, clamped to the text length.
    merged = []
    for off, length in spans:
        start = max(0, min(off, total_units))
        end = max(0, min(off + length, total_units))
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    result = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            result.append(data[cursor * 2:start * 2].decode("utf-16-le"))
        span_text = data[start * 2:end * 2].decode("utf-16-le")
        if any(ch.isalpha() and ord(ch) > 0x7F for ch in span_text):
            log.info("Bold span contains non-Latin text (e.g. Bangla); Unicode "
                     "bold has no glyphs for that script -- posting it unstyled: "
                     "%r", span_text[:60])
        result.append(to_unicode_bold(span_text))
        cursor = end
    if cursor < total_units:
        result.append(data[cursor * 2:].decode("utf-16-le"))
    return "".join(result)


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


def compute_body(raw_caption, entities=None):
    """Build the Facebook-bound text from a Telegram caption.

    When TRANSLATE is enabled, translate to Bangla (which has no Unicode bold,
    so bolding is skipped). Otherwise apply bold entities to the raw text first
    -- so the UTF-16 offsets stay valid against exactly what Telegram sent --
    then strip the trailing promo/handle footer.
    """
    if translation_enabled():
        return translate_text(clean_source_text(raw_caption or ""))
    styled = apply_bold_entities(raw_caption or "", entities or [])
    cleaned = clean_source_text(styled)
    if raw_caption and cleaned != raw_caption:
        log.debug("Applied bold/stripped footer from source text.")
    return cleaned


def overlay_logo(image_path):
    """Overlay logo.png onto the bottom-right corner of the image (in place)."""
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


def _ffprobe_width(video_path):
    """Return the video's pixel width via ffprobe, or None if unavailable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=60,
        )
        return int(out.stdout.strip().splitlines()[0])
    except (subprocess.SubprocessError, ValueError, IndexError, OSError) as exc:
        log.debug("ffprobe width lookup failed: %s", exc)
        return None


def watermark_video(input_path, output_path):
    """Burn logo.png into the bottom-right corner of a video via ffmpeg.

    Returns the output path on success, or the input path (unwatermarked) if
    ffmpeg is unavailable/fails -- so posting still proceeds.
    """
    if not os.path.exists(LOGO_FILE):
        log.warning("Logo file %s not found; posting video without watermark.",
                    LOGO_FILE)
        return input_path

    width = _ffprobe_width(input_path)
    if width:
        logo_w = max(1, int(width * LOGO_SCALE))
        filt = (f"[1:v]scale={logo_w}:-1[wm];"
                f"[0:v][wm]overlay=W-w-{LOGO_MARGIN}:H-h-{LOGO_MARGIN}")
    else:
        # Fallback: scale the logo relative to the main video with scale2ref.
        filt = (f"[1:v][0:v]scale2ref=w=main_w*{LOGO_SCALE}:h=ow/mdar[wm][vid];"
                f"[vid][wm]overlay=W-w-{LOGO_MARGIN}:H-h-{LOGO_MARGIN}")

    # Encode a Facebook-friendly MP4: H.264 + yuv420p pixel format, AAC audio,
    # and +faststart (moov atom at the front) so the Graph API's streaming
    # upload accepts it. Without these, /videos rejects the file with error
    # 6000 / subcode 1363048 ("problem uploading your video file").
    cmd = ["ffmpeg", "-y", "-i", input_path, "-i", LOGO_FILE,
           "-filter_complex", filt,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "128k",
           output_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=VIDEO_UPLOAD_TIMEOUT)
        if proc.returncode == 0 and os.path.exists(output_path):
            log.info("Watermarked video -> %s", output_path)
            return output_path
        log.error("ffmpeg watermark failed (rc=%s): %s",
                  proc.returncode, proc.stderr[-500:] if proc.stderr else "")
        return input_path
    except (subprocess.SubprocessError, OSError) as exc:
        log.error("ffmpeg not available or crashed (%s); posting original video.",
                  exc)
        return input_path


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


def post_photo(page_id, page_token, image_path, caption):
    """Publish a single photo with caption to the Facebook Page."""
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


def _upload_unpublished_photo(page_id, page_token, image_path):
    """Upload a photo with published=false; return its media id or None."""
    url = f"{GRAPH_API}/{page_id}/photos"
    try:
        with open(image_path, "rb") as fh:
            resp = requests.post(
                url,
                data={"published": "false", "access_token": page_token},
                files={"source": fh},
                timeout=REQUEST_TIMEOUT,
            )
        _log_graph_response(resp)
        if resp.ok:
            return resp.json().get("id")
        return None
    except (requests.RequestException, ValueError, OSError) as exc:
        log.error("Failed to upload album photo: %s", exc)
        return None


def post_album(page_id, page_token, image_paths, message):
    """Upload each photo unpublished, then create one multi-photo feed post."""
    media_ids = []
    for path in image_paths:
        media_id = _upload_unpublished_photo(page_id, page_token, path)
        if media_id:
            media_ids.append(media_id)
        else:
            log.warning("Album photo upload failed for %s; continuing.", path)

    if not media_ids:
        log.error("No album photos uploaded successfully; aborting album post.")
        return False

    data = {"message": message or "", "access_token": page_token}
    for i, media_id in enumerate(media_ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": media_id})

    url = f"{GRAPH_API}/{page_id}/feed"
    try:
        resp = requests.post(url, data=data, timeout=REQUEST_TIMEOUT)
        _log_graph_response(resp)
        return resp.ok
    except requests.RequestException as exc:
        log.error("Failed to create album feed post: %s", exc)
        return False


def post_video(page_id, page_token, video_path, description):
    """Upload a video with description to the Facebook Page."""
    url = f"{GRAPH_API}/{page_id}/videos"
    try:
        with open(video_path, "rb") as fh:
            resp = requests.post(
                url,
                data={"description": description or "", "access_token": page_token},
                files={"source": fh},
                timeout=VIDEO_UPLOAD_TIMEOUT,
            )
        _log_graph_response(resp)
        return resp.ok
    except (requests.RequestException, OSError) as exc:
        log.error("Failed to post video to Facebook: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Item processing
# --------------------------------------------------------------------------- #

def _cleanup(*paths):
    """Remove temporary files if they exist."""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            log.warning("Could not remove temp file %s: %s", path, exc)


def _save_dry_run_copy(src, dest):
    """Copy a processed file to a stable inspection path. Returns dest or src."""
    try:
        shutil.copyfile(src, dest)
        return dest
    except OSError as exc:
        log.warning("Could not save dry-run copy: %s", exc)
        return src


def process_item(item, config, page_id, dry_run):
    """Post (or simulate posting) one queue item.

    Returns one of: "posted", "dry_run", "skipped", "failed", "dead".
    "dead" means non-retryable (e.g. oversized video) -> dead-letter immediately.
    """
    bot_token = config["TELEGRAM_BOT_TOKEN"]
    page_token = config["FACEBOOK_PAGE_TOKEN"]
    item_type = item.get("type")
    body = compute_body(item.get("caption"), item.get("entities"))

    if dry_run:
        # Show raw vs. transformed side by side so bold spans are easy to verify.
        log.info("[DRY-RUN] RAW  Telegram text : %s", item.get("caption"))
        log.info("[DRY-RUN] BOLD Facebook text : %s", body)

    # ----- text ----------------------------------------------------------- #
    if item_type == "text":
        if not body or not body.strip():
            log.info("Text item has no content after cleaning; skipping.")
            return "skipped"
        if dry_run:
            log.info("[DRY-RUN] Would post TEXT: %s", body)
            return "dry_run"
        return "posted" if post_text(page_id, page_token, body) else "failed"

    # ----- photo / album -------------------------------------------------- #
    if item_type in ("photo", "album"):
        file_ids = item.get("photo_file_ids") or []
        local_paths = []
        for i, fid in enumerate(file_ids):
            dest = f"{TEMP_MEDIA_PREFIX}{i}.jpg"
            path, _too_big = download_file(bot_token, fid, dest)
            if path:
                overlay_logo(path)
                local_paths.append(path)
            else:
                log.warning("Failed to download album photo %d/%d.",
                            i + 1, len(file_ids))

        if not local_paths:
            log.warning("No photos downloaded for %s item; will retry.", item_type)
            return "failed"

        if len(local_paths) == 1:
            if dry_run:
                saved = _save_dry_run_copy(local_paths[0], DRY_RUN_IMAGE)
                log.info("[DRY-RUN] Would post PHOTO (logo applied) -> %s", saved)
                log.info("[DRY-RUN] Caption: %s", body)
                _cleanup(*local_paths)
                return "dry_run"
            ok = post_photo(page_id, page_token, local_paths[0], body)
            _cleanup(*local_paths)
            return "posted" if ok else "failed"

        # multiple photos -> album
        if dry_run:
            for i, path in enumerate(local_paths):
                _save_dry_run_copy(path, f"dry_run_output_{i}.jpg")
            log.info("[DRY-RUN] Would post ALBUM of %d photos "
                     "(logo applied to each).", len(local_paths))
            log.info("[DRY-RUN] Caption: %s", body)
            _cleanup(*local_paths)
            return "dry_run"
        ok = post_album(page_id, page_token, local_paths, body)
        _cleanup(*local_paths)
        return "posted" if ok else "failed"

    # ----- video ---------------------------------------------------------- #
    if item_type == "video":
        size = item.get("video_file_size")
        if isinstance(size, int) and size > VIDEO_MAX_BYTES:
            log.warning("Video is %.1f MB, over the %d MB Bot API limit; "
                        "dead-lettering.", size / 1e6, VIDEO_MAX_BYTES // (1024 * 1024))
            return "dead"

        path, too_big = download_file(bot_token, item.get("video_file_id"),
                                      TEMP_VIDEO)
        if not path:
            if too_big:
                log.warning("Telegram reports the video is too big to download; "
                            "dead-lettering.")
                return "dead"
            log.warning("Video download failed; will retry.")
            return "failed"

        out = watermark_video(path, WATERMARKED_VIDEO)
        if dry_run:
            saved = _save_dry_run_copy(out, DRY_RUN_VIDEO)
            log.info("[DRY-RUN] Would post VIDEO (watermarked) -> %s", saved)
            log.info("[DRY-RUN] Description: %s", body)
            _cleanup(TEMP_VIDEO, WATERMARKED_VIDEO)
            return "dry_run"
        ok = post_video(page_id, page_token, out, body)
        _cleanup(TEMP_VIDEO, WATERMARKED_VIDEO)
        return "posted" if ok else "failed"

    log.warning("Unknown item type %r; dead-lettering.", item_type)
    return "dead"


def _posted_label(item):
    """Human label of an item's kind for the summary line."""
    t = item.get("type")
    if t == "photo":
        return "single photo"
    if t == "album":
        return "album"
    if t == "video":
        return "video"
    return "text"


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
        help="Prepare posts but do not publish, or mutate state/queue.",
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
    per_run = posts_per_run()

    log.info("Starting Telegram -> Facebook run. dry_run=%s posts_per_run=%s",
             dry_run, per_run)
    config = load_config()

    telegram_ok, _facebook_ok = run_health_check(config)
    if args.health:
        log.info("Health check complete (--health); exiting.")
        return 0 if telegram_ok else 1

    # ----- 1. Fetch new Telegram updates and build grouped items ---------- #
    last_update_id = load_state()
    offset = last_update_id + 1 if last_update_id else 0
    updates = get_updates(config["TELEGRAM_BOT_TOKEN"], offset)
    fetched = len(updates)
    new_items = build_items(updates)
    newest_update_id = max(
        (uid for u in updates if (uid := u.get("update_id")) is not None),
        default=last_update_id,
    )

    queue = load_queue()
    queue_before = len(queue)
    page_id = resolve_page_id(config["FACEBOOK_PAGE_TOKEN"])

    counts = {"posted": 0, "skipped": 0, "failed": 0, "dead": 0}
    posted_types = []

    if dry_run:
        # Never mutate state or the queue. Simulate posting the effective front.
        log.info("[DRY-RUN] Would enqueue %d new item(s); offset would advance "
                 "to %s.", len(new_items), newest_update_id)
        # Preview the bold transformation for every freshly fetched item, so new
        # posts are visible even though only the queue front gets fully simulated.
        for idx, new_item in enumerate(new_items):
            preview = compute_body(new_item.get("caption"), new_item.get("entities"))
            log.info("[DRY-RUN] New item %d (%s) RAW : %s",
                     idx + 1, new_item.get("type"), new_item.get("caption"))
            log.info("[DRY-RUN] New item %d (%s) BOLD: %s",
                     idx + 1, new_item.get("type"), preview)
        effective = queue + new_items
        if effective:
            front = effective[0]
            result = process_item(front, config, page_id, dry_run=True)
            if result == "dry_run":
                posted_types.append(_posted_label(front))
                log.info("[DRY-RUN] Would react %s to Telegram message(s) %s "
                         "in chat %s.", REACTION_EMOJI,
                         front.get("message_ids"), front.get("chat_id"))
            counts[result] = counts.get(result, 0) + 1
        else:
            log.info("[DRY-RUN] Queue is empty; nothing to simulate.")
        _log_summary(fetched, len(new_items), queue_before, len(queue),
                     counts, posted_types, dry_run)
        return 0

    # ----- 2. Enqueue and advance offset (regardless of downstream) ------- #
    if new_items:
        queue.extend(new_items)
        save_queue(queue)
        log.info("Enqueued %d new item(s); queue length now %d.",
                 len(new_items), len(queue))
    if newest_update_id > last_update_id:
        save_state(newest_update_id)

    # ----- 3. Post up to POSTS_PER_RUN items from the front (FIFO) -------- #
    for _ in range(per_run):
        if not queue:
            break
        item = queue[0]
        try:
            result = process_item(item, config, page_id, dry_run=False)
        except Exception as exc:  # one bad item must not crash the run
            log.exception("Unexpected error processing item: %s", exc)
            result = "failed"

        if result == "posted":
            posted_types.append(_posted_label(item))
            # React to the source Telegram message(s) on confirmed success.
            react_to_messages(config["TELEGRAM_BOT_TOKEN"],
                              item.get("chat_id"), item.get("message_ids"))
            queue.pop(0)
            save_queue(queue)
            counts["posted"] += 1
        elif result == "skipped":
            queue.pop(0)
            save_queue(queue)
            counts["skipped"] += 1
        elif result == "dead":
            dead_item = queue.pop(0)
            dead_letter(dead_item)
            save_queue(queue)
            counts["dead"] += 1
        else:  # failed -> retry accounting, keep at front
            counts["failed"] += 1
            item["retry_count"] = item.get("retry_count", 0) + 1
            log.warning("Item failed (attempt %d/%d, type=%s).",
                        item["retry_count"], MAX_RETRIES, item.get("type"))
            if item["retry_count"] >= MAX_RETRIES:
                dead_item = queue.pop(0)
                dead_letter(dead_item)
                counts["dead"] += 1
            save_queue(queue)
            # Front is blocked; stop this run rather than spin on the same item.
            break

    _log_summary(fetched, len(new_items), queue_before, len(queue),
                 counts, posted_types, dry_run)
    return 0


def _log_summary(fetched, enqueued, queue_before, queue_after, counts,
                 posted_types, dry_run):
    log.info(
        "SUMMARY: fetched=%d enqueued=%d queue_before=%d queue_after=%d "
        "posted=%d skipped=%d failed=%d dead_letter=%d posted_types=%s dry_run=%s",
        fetched, enqueued, queue_before, queue_after,
        counts.get("posted", 0), counts.get("skipped", 0),
        counts.get("failed", 0), counts.get("dead", 0),
        posted_types or "[]", dry_run,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - top-level safety net
        log.exception("Fatal error: %s", exc)
        sys.exit(1)
