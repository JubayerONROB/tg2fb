# Telegram → Facebook Cross-Poster

Automatically mirrors posts from a Telegram channel to a Facebook Page. New
channel posts are fetched hourly into a persistent queue, and a rate-limited
number of them (one per hour by default) are published to your Facebook Page —
text, single photos, photo albums, and videos, each stamped with your
`logo.png`. State lives in `state.json` (fetch offset) and `queue.json` (pending
items) so nothing is ever re-posted or lost.

## How it works

1. `app.py` calls the Telegram Bot API's `getUpdates` with an `offset` derived
   from the last processed `update_id` (stored in `state.json`, default `0`).
2. New `channel_post` updates are grouped into queue items — album photos that
   share a `media_group_id` are merged into one item — and appended to
   `queue.json`. Only stable Telegram `file_id`s are stored (never expiring
   download URLs). The fetch offset is then advanced immediately, so nothing is
   ever re-fetched, regardless of what happens next.
3. The pipeline pops up to `POSTS_PER_RUN` items (default `1`) from the front of
   the queue (FIFO) and posts them:
   - **text** → `/{page-id}/feed`
   - **single photo** → logo overlaid → `/{page-id}/photos`
   - **album** → each photo logo-overlaid and uploaded unpublished, then one
     multi-photo post via `/{page-id}/feed` with `attached_media`
   - **video** → logo burned in as a watermark with `ffmpeg` → `/{page-id}/videos`
4. Captions have any trailing `@handle` promo footer stripped, and are optionally
   translated to Bangla (see the `TRANSLATE` env var).
5. An item is removed from the queue only on confirmed success. On failure its
   `retry_count` is incremented and it stays at the front; after 3 failed
   attempts it is moved to `queue_dead_letter.json` so it never blocks the queue.

## Queue & rate limiting

Posts are **spread out over time** rather than published all at once. Because
the scheduled workflow runs hourly and posts one item per run by default, a
burst of ten Telegram posts becomes ten Facebook posts over ten hours — which
looks natural on a Page and avoids tripping spam heuristics.

- **`queue.json`** holds pending items; **`state.json`** holds only the Telegram
  fetch offset. They are deliberately decoupled: fetching never depends on
  posting succeeding.
- **Change the rate** with the `POSTS_PER_RUN` env var (integer, default `1`).
  For example `POSTS_PER_RUN=3` posts up to three queued items per hourly run.
- **`queue_dead_letter.json`** collects items that failed 3 times or can't be
  posted (e.g. an oversized video), so one bad item never stalls everything.

### Video size limit

The Telegram Bot API can only download files up to **20 MB** via `getFile`.
Videos larger than that cannot be fetched, so they are logged and moved straight
to the dead-letter queue instead of crashing the run. Videos within the limit
are watermarked with `ffmpeg` (installed by the workflow) and uploaded.

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

Optional variables:

| Variable    | Default | Description                                                     |
| ----------- | ------- | --------------------------------------------------------------- |
| `TRANSLATE` | off     | Set to `1`/`true` to translate posts to Bangla via Google Translate. When unset, the original text is posted as-is (trailing `@handle` promo footer still stripped). |
| `DRY_RUN`   | off     | Set to `1`/`true` to prepare posts without publishing or advancing state. |

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

## Automation

A few things to know about how the hourly schedule behaves:

- **Default branch only.** GitHub runs `schedule` workflows *only* from the
  default branch (`main`). The workflow file must be committed to `main` — a
  scheduled run will never fire from a feature branch.
- **Cron is best-effort.** The schedule (`17 * * * *`, hourly at :17 UTC) is a
  hint, not a guarantee. GitHub can delay runs — often by several minutes at
  busy times — and occasionally skip one under heavy load. An off-peak minute
  (`:17` instead of `:00`) reduces the top-of-hour queue delay, but don't rely
  on exact timing.
- **60-day inactivity auto-disable + heartbeat.** GitHub automatically disables
  scheduled workflows after **60 days with no repository activity**. To keep a
  quiet channel from silently killing the automation, every real (non-dry-run)
  run writes the current UTC timestamp to `.github/last_run.txt` and commits it
  back — even when there were no new Telegram posts. That regular commit keeps
  the repo "active" so the schedule is never auto-disabled.
- **Confirm it's live.** After pushing to `main`, trigger one manual run
  (**Actions → Run workflow**) to register the workflow and confirm it's
  enabled. Scheduled runs only start appearing once GitHub has seen the
  workflow on the default branch.

## Testing

You can test the whole pipeline safely without ever posting to Facebook.

### Dry-run mode

When `DRY_RUN` is enabled (env var `1`/`true`, or the `--dry-run` flag) the
script does everything **except** the actual Facebook POST:

- It fetches, translates to Bangla, downloads photos and applies the logo.
- It logs exactly what *would* be posted (the Bangla text, and whether a photo
  with logo was prepared).
- The processed image is saved locally as `dry_run_output.jpg` so you can open
  and inspect it.
- `state.json` is **not** advanced, so you can re-run against the same post
  repeatedly.

Useful flags:

- `--dry-run` — enable dry-run (same as `DRY_RUN=1`).
- `--verbose` / `-v` — DEBUG logging so each step is visible.
- `--health` — only validate both tokens (`getMe` + Graph `/me`) and exit.
- `--once` — explicit single pass (this is the default behavior).

### Locally (with a `.env`)

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHANNEL_ID="@yourchannel"
export FACEBOOK_PAGE_TOKEN="..."

# Safe test — prepares everything, posts nothing, keeps state untouched:
DRY_RUN=1 python app.py --verbose
# or:
python app.py --dry-run --verbose

# Just check that both tokens are valid:
python app.py --health
```

Then open `dry_run_output.jpg` to check the logo placement.

### From the Actions tab

Go to **Actions → Hourly Telegram to Facebook Cross-Post → Run workflow**. The
manual trigger exposes a **`dry_run`** checkbox that defaults to **true**, so a
manual test never posts to Facebook and never commits `state.json`. Untick it
only when you want the manual run to publish for real.

> The **scheduled** hourly run always uses `DRY_RUN=false` and posts for real.

Every run ends with a one-line summary, e.g.:

```
SUMMARY: fetched=3 enqueued=3 queue_before=0 queue_after=2 posted=1 skipped=0 failed=0 dead_letter=0 posted_types=['single photo'] dry_run=False
```

`posted_types` shows which kind was published (text / single photo / album /
video), and `queue_before`/`queue_after` show the queue length around the run.

## Files

| File                                | Purpose                                  |
| ----------------------------------- | ---------------------------------------- |
| `app.py`                            | Main cross-posting script.               |
| `requirements.txt`                  | Python dependencies.                     |
| `.github/workflows/hourly_run.yml`  | Hourly scheduled GitHub Actions job.     |
| `.github/last_run.txt`              | Heartbeat timestamp; keeps the schedule alive (see Automation). |
| `state.json`                        | Telegram fetch offset (last processed `update_id`). |
| `queue.json`                        | Persistent FIFO queue of pending, not-yet-posted items. |
| `queue_dead_letter.json`            | Items that failed 3× or can't be posted (created on demand). |
| `logo.png`                          | Watermark overlaid on photos and videos. |
| `dry_run_output.jpg` / `.mp4`       | Processed media saved during a dry-run (git-ignored). |

## Security

No tokens are hardcoded — everything comes from environment variables /
GitHub secrets. Never commit real tokens. If a token is ever exposed, rotate it
immediately.
