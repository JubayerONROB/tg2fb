# Telegram → Facebook Cross-Poster

Automatically mirrors posts from a Telegram channel to a Facebook Page. New
channel posts are fetched hourly, their text/caption is translated to Bangla,
your `logo.png` is stamped onto any attached photo, and the result is published
to your Facebook Page. Processing state lives in `state.json` so nothing is ever
re-posted.

## How it works

1. `app.py` calls the Telegram Bot API's `getUpdates` with an `offset` derived
   from the last processed `update_id` (stored in `state.json`, default `0`).
2. Each new `channel_post` is translated to Bangla with `deep-translator`
   (falls back to the original text if translation fails).
3. Photos are downloaded at the highest resolution, and `logo.png` is overlaid
   on the bottom-right corner (scaled to ~15% of the image width, with a 15px
   margin, preserving transparency).
4. Content is published to the Facebook Page via the Meta Graph API (`v21.0`) —
   `/{page-id}/photos` for images, `/{page-id}/feed` for text.
5. `state.json` is updated with the newest `update_id`.

## Setup

### 1. Requirements

```bash
pip install -r requirements.txt
```

Make sure `logo.png` exists in the repository root (already included).

### 2. Environment variables

The script requires three variables — it fails loudly if any are missing:

| Variable              | Description                                              |
| --------------------- | -------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | Bot token from [@BotFather](https://t.me/BotFather). The bot must be an admin of the channel. |
| `TELEGRAM_CHANNEL_ID` | Your channel id / username (used for reference).         |
| `FACEBOOK_PAGE_TOKEN` | A **Page access token** with `pages_manage_posts` and `pages_read_engagement`. |

For local runs, create a `.env` (git-ignored) or export them directly:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHANNEL_ID="@yourchannel"
export FACEBOOK_PAGE_TOKEN="..."
python app.py
```

> **Note:** the Telegram bot must be added as an **administrator** of the
> channel, otherwise `getUpdates` will not deliver `channel_post` updates.

### 3. Run automatically (GitHub Actions)

The workflow in `.github/workflows/hourly_run.yml` runs every hour and can also
be triggered manually from the **Actions** tab (`workflow_dispatch`).

Add the three variables as repository secrets under
**Settings → Secrets and variables → Actions**:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHANNEL_ID`
- `FACEBOOK_PAGE_TOKEN`

The workflow commits any change to `state.json` back to the repo so progress
persists between runs. It needs `contents: write` permission (already set in the
workflow).

## Files

| File                                | Purpose                                  |
| ----------------------------------- | ---------------------------------------- |
| `app.py`                            | Main cross-posting script.               |
| `requirements.txt`                  | Python dependencies.                     |
| `.github/workflows/hourly_run.yml`  | Hourly scheduled GitHub Actions job.     |
| `state.json`                        | Auto-generated processing state.         |
| `logo.png`                          | Watermark overlaid on photos.            |

## Security

No tokens are hardcoded — everything comes from environment variables /
GitHub secrets. Never commit real tokens. If a token is ever exposed, rotate it
immediately.
