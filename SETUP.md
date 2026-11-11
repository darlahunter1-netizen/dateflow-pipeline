# DateFlow Pipeline - Setup and Operations

This guide matches the current workflow. Production runs **only** in GitHub Actions.
Do not run `pipeline.py` on Windows or on a local machine as the production path.

## 1. Keep the repository public

GitHub Release assets are only publicly downloadable from a public repository. The
workflow uploads MP4s to a monthly release tag `dateflow-media-YYYY-MM`; Buffer must be able to reach
the media URL without authentication. Do not make the repository private.

## 2. Add GitHub Actions secrets

Go to **Settings -> Secrets and variables -> Actions -> Secrets -> New repository secret**.
Add names only; never paste a value into a file, issue or chat.

| Secret | Required | What it is |
|---|---|---|
| `PEXELS_API_KEY` | Production | Pexels API key used by MoneyPrinterTurbo for b-roll |
| `GEMINI_API_KEY` | Optional | Free Gemini API key used for script generation; the fallback pool works without it |
| `BUFFER_API_KEY` | Production | Buffer API key from <https://publish.buffer.com/settings/api> |
| `BUFFER_ORG_ID` | Production | Buffer organization id |
| `BUFFER_CHANNEL_ID` | Production | Buffer channel id for the Instagram account |
| `TELEGRAM_BOT_TOKEN` | Optional | Telegram bot token for one summary per production run |
| `ADMIN_CHAT_ID` | Optional | Telegram chat that receives the summary |

`GITHUB_TOKEN` is created by Actions automatically. The workflow grants it
`contents: write` so it can create release assets. If your organization blocks write
tokens, change **Settings -> Actions -> General -> Workflow permissions** to allow read and
write, or the release upload step will fail.

Optional **repository variable** (Settings -> Secrets and variables -> Actions -> Variables):

| Variable | Purpose |
|---|---|
| `PUBLIC_MEDIA_BASE_URL` | Direct HTTPS prefix that serves the MP4 as `video/mp4` (GitHub Pages, public R2 bucket, etc.) |
| `REPLACE_ERRORED_SLOTS` | `0` keeps failed Buffer posts (the slot is then blocked); default `1` deletes and retries them |

## 3. Read the Buffer ids without exposing them

Run these read-only commands in a private terminal where `BUFFER_API_KEY` is already
exported. Do not put the output in the repository.

```bash
export BUFFER_API_KEY="..."   # private terminal only
buffer account --output json
buffer channels list --organization-id "<org-id>" --output json
```

Copy the organization id and the Instagram channel id directly into the two GitHub
Actions secrets. Do not commit them, and do not paste them into an issue.

The pipeline validates that the channel exists indirectly: if the id is wrong, Buffer
returns an error and the run fails non-zero.

## 4. Choose the media delivery path

The pipeline always uploads to the public monthly GitHub Release `dateflow-media-YYYY-MM`. That release is
the durable archive and the default URL handed to Buffer.

GitHub serves release assets through a redirect with `Content-Type:
application/octet-stream` and `Content-Disposition: attachment`. Buffer's documentation
asks for a direct, stable URL and fetches media at publish time. If Buffer reports a media
URL error, use a direct `video/mp4` prefix and set `PUBLIC_MEDIA_BASE_URL`:

### Option A - GitHub Pages

1. Create a small public Pages site in the same repository (or a dedicated media repo)
   that publishes the rendered MP4 files under a stable path.
2. Enable **Settings -> Pages** and deploy that site.
3. Set `PUBLIC_MEDIA_BASE_URL` to the site prefix, for example
   `https://<owner>.github.io/<repo>/media`.
4. The pipeline verifies that
   `https://<owner>.github.io/<repo>/media/reel_YYYY-MM-DD_HHMM.mp4` is a direct
   `video/mp4` response before it gives the URL to Buffer.

### Option B - Public Cloudflare R2 bucket

1. Create an R2 bucket and a public custom domain or public bucket URL.
2. Upload the rendered MP4s there from your own deployment step, or configure your own
   mirror to copy the release asset.
3. Set `PUBLIC_MEDIA_BASE_URL` to the public prefix.

When the variable is set, the pipeline requires the URL to be direct (no redirect) and
served as `video/mp4`. The pipeline does **not** deploy Pages or upload to R2 itself; it
only verifies the URL, so you must mirror each rendered file to that prefix with your own
step. When it is not set, the GitHub Release URL is used and a warning is
logged. Temporary hosts, expiring links and signed URLs are not supported as the active
path.

## 5. Enable Actions

1. Open the **Actions** tab and enable workflows if GitHub asks.
2. Open **DateFlow Reels**.
3. Check that the schedule shows the three cron entries:
   * `10 20 * * *` (23:10 TRT) - primary batch for the next TRT day
   * `10 2 * * *` (05:10 TRT) - recovery
   * `10 8 * * *` (11:10 TRT) - later-slot recovery

The primary batch exists because GitHub can start scheduled jobs 5-8 hours late. It runs
late in the TRT evening so that even a long delay still lands before 11:00 TRT. Recovery
runs only fill slots that are still in the future. A slot that has already passed cannot
be backfilled by a delayed run.

GitHub also disables scheduled workflows in a public repository after **60 days without
repository activity**. The separate `Keepalive` workflow commits one timestamp file on the
first of each month to keep the schedule alive. If Actions shows a disabled workflow, open
it and click **Enable workflow**.

## 6. First test: dry-run

1. **Actions -> DateFlow Reels -> Run workflow**.
2. Leave `mode=dry_run` and `confirm` empty.
3. Run it.

A dry-run:

* reads the Buffer queue if the Buffer secrets are configured (read-only),
* writes one original script per free future slot,
* validates the real `posts create` payload with `buffer posts create --dry-run`,
* writes `output/dry_run_plan.json` as an artifact,
* does **not** render, upload to a release, or create a Buffer post.

If no future slot is free, it exits successfully and prints that there is nothing to do.

## 7. First real catch-up

Only after the dry-run looks correct:

1. **Run workflow**.
2. Select `mode=production`.
3. Type `PUBLISH` in `confirm`.
4. Optionally set `date` to a TRT date (`YYYY-MM-DD`) and `max_posts` to 1-4.

Without `confirm=PUBLISH` the workflow stops before reading production secrets. Scheduled
runs do not need the confirmation input because they are not manual.

## 8. What a production run does

For each free future slot:

1. Generates or selects a script.
2. Renders with MoneyPrinterTurbo v1.3.10 at 1080x1920 / 9:16.
3. Runs `ffprobe` and rejects a wrong size, codec, missing audio or duration.
4. Uploads to the public GitHub Release.
5. Verifies the media URL and MP4 bytes.
6. Validates the Buffer payload locally with `--dry-run`.
7. Checks the slot again for occupancy.
8. Creates the post with the official Buffer CLI.
9. Reads the post back and requires status `scheduled` (or `buffer`).

At the end it sends **one** Telegram summary if Telegram is configured. A run with no free
slots sends nothing. A real failure sends one error summary and exits non-zero so the
Actions run is red, not falsely green.

## 9. Buffer free-plan limits

* Up to **10 scheduled posts per channel at one time**.
* Up to **1,000 API requests per channel per month** (3,000 with three channels).
* Four posts per day fits; the pipeline caps the queue at 10 and skips slots when the
  queue is full instead of crashing. Empty recovery runs make a single read-only call,
  so a normal day stays around 20 requests.

Buffer's `posts list` default fields do not include `dueAt`. The pipeline requests
`items.{id,status,dueAt,channelId,text}` explicitly. Without that, every slot would look
free and every run would create duplicates.

If Buffer leaves a future post in `error` status, the pipeline deletes that broken post and
re-creates the slot on the next run so the automation can recover. Set the repository
variable `REPLACE_ERRORED_SLOTS=0` if you prefer to clean those posts by hand; the slot is
then reported as blocked and the run fails loudly instead.

## 10. Rotate exposed credentials

The current files no longer contain values, but **earlier commits in this public
repository did**. Removing a string from the current file does not remove it from Git
history, forks, caches or search indexes. Rotate the credentials now:

1. **Telegram bot token** - open BotFather, select the bot, use **Revoke current token**,
   then update `TELEGRAM_BOT_TOKEN` in GitHub Actions secrets.
2. **Pexels API key** - regenerate it in the Pexels dashboard, then update
   `PEXELS_API_KEY`.
3. **DeepSeek / OpenAI API key** - revoke the key in the provider dashboard. The pipeline
   no longer uses a paid LLM; `GEMINI_API_KEY` is the optional free replacement.
4. **Buffer API key** - regenerate it at <https://publish.buffer.com/settings/api> if it
   was ever pasted into a commit, then update `BUFFER_API_KEY`.
5. **Telegram chat id / Buffer organization and channel ids** - these identify an account
   but do not authorize access. Treat them as private configuration anyway; they are now
   supplied only through secrets.

After rotating, update every GitHub Actions secret. This project did **not** force-push or
rewrite history, because that requires a separate decision and coordination with anyone
who cloned the repository. If you later choose a history rewrite, do it after rotating
the keys; a rewrite alone does not make leaked keys safe.

## 11. Troubleshooting

| Symptom | Check |
|---|---|
| `AUTH_ERROR` from Buffer | `BUFFER_API_KEY` secret is missing, revoked or copied with whitespace |
| `Missing required field organizationId` | The pipeline must send the org id inside `--json`; do not mix `--json` with `--organization-id` |
| Buffer media URL error | Set `PUBLIC_MEDIA_BASE_URL` to a direct `video/mp4` host; GitHub Release URLs redirect and are served as octet-stream |
| Post status `needs_approval` or `draft` | Buffer approval workflow is enabled or `save-to-draft` was used; the pipeline treats this as a failed slot |
| Queue full / 10 scheduled posts | Wait for posts to publish, or reduce `max_posts`; Buffer Free is a queue, not a monthly quota |
| MPT render failed | Check the Actions log for the failed stage; Pexels or Edge TTS outages are retried on the next recovery run |
| Scheduled run started hours late | Expected on GitHub-hosted cron. The primary batch is placed early enough for an 8-hour delay; a slot that already passed cannot be backfilled |
| Workflow shows as disabled | GitHub disables schedules after 60 days without repository activity. Click **Enable workflow**; `Keepalive` normally prevents this |
| Need a one-off Buffer diagnosis | Run `buffer doctor` in the Actions log or in a private terminal; it never prints the key |
| No Telegram message | Empty runs intentionally send nothing. Check that both Telegram secrets are set for failures |
| Release upload 403 | The workflow needs `contents: write`; check the repository Actions permissions |

## 12. Local checks that are safe

These commands do not publish and do not need production secrets:

```bash
python -m py_compile pipeline.py
bash -n setup_buffer.sh
python -m unittest discover -s tests -v
python pipeline.py --mode dry_run --offline --no-llm --max-posts 1
```

Do not use a local run as the production path. The supported production path is the
GitHub Actions workflow.

## 13. Files and schedule reference

| File | Purpose |
|---|---|
| `pipeline.py` | Planner, scripts, MPT render, release upload, Buffer scheduling |
| `.github/workflows/daily-reel.yml` | Production schedule and manual inputs |
| `.github/workflows/tests.yml` | Offline tests and syntax checks |
| `.github/workflows/keepalive.yml` | Monthly heartbeat commit so GitHub keeps the schedule enabled |
| `setup_buffer.sh` | Preflight; the API key is read from the environment only |
| `tests/test_pipeline.py` | Offline unit tests |

Posting slots are **11:00, 15:00, 18:15 and 21:30 TRT** (08:00, 12:00, 15:15 and 18:30 UTC).
