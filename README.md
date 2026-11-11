# DateFlow Reels Autopilot

Four English-language Instagram Reels per day for **@dataflow_planner**, rendered with
**MoneyPrinterTurbo** and scheduled through the **official Buffer CLI**.

Production runs only inside GitHub Actions. There is no supported local Windows workflow.
The repository is public on purpose: GitHub Release assets are only publicly downloadable
from a public repository.

## What a run does

```
GitHub Actions (schedule or manual dry-run)
        |
        v
pipeline.py
  |-- reads the Buffer queue (read-only, explicit dueAt fields)
  |-- writes original scripts (free Gemini API, else reviewed fallback pool)
  |-- renders 1080x1920 / 9:16 with MoneyPrinterTurbo v1.3.10
  |-- uploads the MP4 to a public GitHub Release "dateflow-media-YYYY-MM"
  |-- verifies the public media URL
  |-- creates the post with @bufferapp/cli 2.3.0
  |-- verifies the created post status
        |
        v
Buffer publishes to Instagram at the scheduled TRT time
```

## Posting slots

| Slot (TRT, UTC+3) | Slot (UTC) | Purpose |
|:--:|:--:|---|
| **11:00** | 08:00 | Late-morning scroll |
| **15:00** | 12:00 | Afternoon break |
| **18:15** | 15:15 | Commute / after work |
| **21:30** | 18:30 | Evening peak |

Turkey has used UTC+3 year-round since 2016, so these offsets are stable.

## Workflow schedule and its limits

GitHub's scheduler is not exact. An audit of this repository on 2026-11-10/11 found
scheduled runs starting **5-8 hours late**. The old four just-in-time cron jobs therefore
missed slots. The current workflow uses one early batch plus two recovery runs:

| UTC cron | TRT time | Role |
|:--:|:--:|---|
| `10 20 * * *` | 23:10 | **Primary batch**: schedules the next TRT day's four slots in advance |
| `10 2 * * *` | 05:10 | Recovery: fills any of today's slots that are still free |
| `10 8 * * *` | 11:10 | Recovery for later slots only; 11:00 cannot be backfilled |

With the observed 8-hour delay, the primary batch still starts around 07:10 TRT, before
the 11:00 slot. A run that starts after a slot has passed **cannot** publish that slot
retroactively; the planner only returns future slots. This is an honest limitation of
GitHub-hosted cron.

GitHub also disables scheduled workflows in a public repository after **60 days without
repository activity**. The `Keepalive` workflow writes one timestamp to
`.github/keepalive.txt` on the first day of each month, which keeps the schedule enabled.
If Actions ever shows the workflow as disabled, open **Actions -> DateFlow Reels -> Enable
workflow**.

The workflow is idempotent: every run lists Buffer posts in a narrow due-date window,
skips occupied slots, refuses to write when the list is paginated, and caps the queue at
the Buffer free-plan limit. Empty runs exit **0** and send no Telegram message. Real render,
upload, Buffer or configuration errors exit **non-zero** and produce one Telegram summary.

## Manual runs are dry-run by default

Actions -> **DateFlow Reels** -> **Run workflow**:

* `mode=dry_run` (default) generates scripts, plans free slots and validates the real
  Buffer payload with `buffer posts create --dry-run`. It does **not** render, upload or
  publish.
* `mode=production` requires **`confirm=PUBLISH`**. Without that string the workflow stops
  before any secret is used.
* `date=YYYY-MM-DD` optionally targets a TRT day for a manual catch-up.
* `max_posts` is capped at 4.

Scheduled runs (`event=schedule`) always run in production mode and do not require the
confirmation input.

## Rendering with MoneyPrinterTurbo

MoneyPrinterTurbo is a hard requirement. It is pinned to **v1.3.10** and installed in the
workflow:

* **1080x1920, 9:16** output (`--video-aspect 9:16`, MPT `VideoAspect.portrait`).
* **Word-by-word animated subtitles**: `--subtitle-display-mode word_by_word`,
  `--subtitle-animation pop_spring`, Anton font, 78 px, white text, black stroke.
* **Pexels b-roll**: `--video-source pexels` and per-script search terms.
* **Transitions**: MPT `Shuffle`, which includes fade, slide and zoom transitions.
  `--video-fit-mode cover` fills the vertical canvas.
* **Zoom / Ken Burns**: MPT applies smooth per-frame Ken Burns zoom to image materials
  (`--image-motion zoom`). Pexels clips are video, not images, so they receive MPT's zoom
  transitions instead of a per-clip Ken Burns animation.
* **Voice**: Edge TTS `en-US-GuyNeural`, rate 1.1. No background music is used.

The pipeline runs `ffprobe` after rendering and fails the slot if the file is not
1080x1920, H.264 with an AAC audio track, or outside 8-90 seconds.

## Script generation: free, with a reviewed fallback

* **Primary model:** Google **Gemini 3.8 Flash** through the official OpenAI-compatible
  endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`), model
  `gemini-3.8-flash`.
* **Cost:** the Gemini API free tier currently offers free input/output tokens for this
  model. Free-tier access is limited, limits are **not guaranteed**, and Google may change
  models, quotas or availability. The project does not promise permanent free access.
* **Privacy:** Google states that free-tier content may be used to improve its products.
  The scripts contain no personal data.
* **No paid fallback:** if the free model is missing, rate-limited, down, or returns
  invalid JSON, the pipeline uses a reviewed pool of 16 original scripts. It never calls a
  paid API.
* **Language:** English only. A Turkish mix is not enabled.
* **Quality rules:** a hook in the first 1-2 seconds, a concrete tension or unexpected
  claim, one practical tip, a payoff, and a short low-pressure CTA.
* **Safety:** coercion, pressure, jealousy games, negging, persistence after a no and
  alcohol-based manipulation are rejected by validation. Consent and respect are required.
* **No repetition:** hooks from Buffer's recent history and the current queue are passed
  to the model and checked with fuzzy matching.

## Media hosting: GitHub Release archive, Buffer delivery caveat

Every MP4 is uploaded to a public monthly release tag **`dateflow-media-YYYY-MM`** (for
example `dateflow-media-2026-11`). Monthly releases keep every release far below GitHub's
1,000-asset cap; the legacy `dateflow-media` tag is left untouched. The upload is verified
(asset state, byte size, public URL, MP4 magic bytes).

Important: GitHub serves release assets through a **302 redirect** to a short-lived signed
URL with `Content-Type: application/octet-stream` and `Content-Disposition: attachment`,
regardless of the upload's content type. Buffer's media guide asks for a **direct, stable,
public** URL and fetches the file when the post publishes, not when it is created.

The pipeline defaults to the release URL because it is the durable public archive required
by this project. If Buffer reports a media URL error, configure a direct `video/mp4`
delivery prefix and set the repository variable `PUBLIC_MEDIA_BASE_URL`:

* a GitHub Pages site that serves the rendered files, or
* a public Cloudflare R2 bucket (Buffer's own recommended option), or
* any other permanent HTTPS host that serves `video/mp4` directly.

When `PUBLIC_MEDIA_BASE_URL` is set, the pipeline still uploads to the GitHub Release for
the archive, but gives Buffer `<PUBLIC_MEDIA_BASE_URL>/<asset-name>.mp4` and verifies that
the URL is direct, `video/mp4` and alive. The pipeline does not deploy Pages or upload to
R2; mirroring the MP4s to that prefix is a separate step you own. Temporary file hosts, expiring links and
signed URLs are not used as the active path.

## Buffer limits and duplicate prevention

* Buffer Free: up to **10 scheduled posts per channel** at one time and **1,000 API
  requests per channel per month** (up to 3,000 with three channels). Four posts per day
  fits; the pipeline also enforces `BUFFER_MAX_QUEUE=10`. A normal day uses about 20
  requests because recovery runs that find no free slot stop after the first read.
* The pipeline requests `items.{id,status,dueAt,channelId,text}` explicitly. Buffer's
  default `posts list` field set omits `dueAt`, which previously made every slot look free.
* Occupied slots are detected with a 15-minute tolerance. Real statuses are `draft`,
  `error`, `needs_approval`, `scheduled`, `sending`, `sent` (plus the queued `buffer` value
  accepted by `createPost`). `pending` and `failed` are not Buffer statuses.
* A slot is checked before rendering and again before `posts create`.
* `posts create` is not idempotent. The pipeline never retries it blindly: after an
  ambiguous failure it lists the slot again and only reports success if the post exists.
* `posts.create` must return a post with status `scheduled` (or `buffer`). A post created
  as `needs_approval` or `draft` is treated as a failure for the autopilot, not a success.

## Required GitHub configuration

Add these under **Settings -> Secrets and variables -> Actions -> Secrets**:

| Secret | Purpose |
|---|---|
| `PEXELS_API_KEY` | Pexels b-roll for MoneyPrinterTurbo |
| `GEMINI_API_KEY` | Optional free Gemini key for scripts; fallback pool works without it |
| `BUFFER_API_KEY` | Official Buffer API key |
| `BUFFER_ORG_ID` | Buffer organization id (read-only identifier) |
| `BUFFER_CHANNEL_ID` | Instagram channel id in Buffer |
| `TELEGRAM_BOT_TOKEN` | Optional notifications |
| `ADMIN_CHAT_ID` | Optional Telegram chat id |

Optional **repository variable** (not a secret):

| Variable | Purpose |
|---|---|
| `PUBLIC_MEDIA_BASE_URL` | Direct `video/mp4` prefix for Buffer (GitHub Pages, R2, etc.) |
| `REPLACE_ERRORED_SLOTS` | `0` disables automatic deletion of failed Buffer posts; default `1` |

`GITHUB_TOKEN` is provided by Actions. The workflow declares `permissions: contents: write`
so the token can upload release assets. Never put secret values in the repository, an
issue, a log or a screenshot.

## Testing

Offline tests do not call Buffer, Gemini, Pexels, Telegram or GitHub:

```bash
python -m py_compile pipeline.py
python -m unittest discover -s tests -v
python pipeline.py --mode dry_run --offline --no-llm --max-posts 1
```

The `Tests` workflow runs the same checks on pushes and pull requests and also validates
the YAML and shell syntax. It has read-only permissions.

## Known limitations

* A delayed GitHub schedule cannot backfill a slot that has already passed.
* GitHub Release assets are not a direct `video/mp4` host; see the media caveat above.
* MoneyPrinterTurbo's smooth Ken Burns zoom is implemented for images; Pexels video clips
  get zoom transitions, not a per-clip Ken Burns effect.
* Gemini's free tier is not guaranteed forever. If it changes, the reviewed fallback pool
  keeps the pipeline running; there is no paid fallback.
* Buffer's free queue holds 10 scheduled posts per channel. A full queue is logged as a
  capacity issue and the run exits 0, not as a crash.
* GitHub disables scheduled workflows after 60 days without repository activity; the
  monthly `Keepalive` commit prevents that, but it depends on the same scheduler. If both
  stop, re-enable them manually once.

## Current status of the live account (2026-11-11)

One real post from the old pipeline exists in Buffer: a Reel at **18:15 TRT** in the queue
labelled **Custom**. The new pipeline treats that slot as occupied and will not duplicate
it. Until a new production run happens, the rest of the day is still produced by the old
code path, so expect the older caption style (it includes emoji) on that single post.

## Security note

Earlier commits in this public repository contained real API keys and a Telegram bot
token. Removing them from the current files does **not** remove them from Git history.
Rotate/revoke every exposed credential and update the GitHub Actions secrets. See
[`SETUP.md`](SETUP.md#10-rotate-exposed-credentials). This project does not force-push or
rewrite history.

## Files

| File | Purpose |
|---|---|
| `pipeline.py` | Slot planner, script generation, MPT render, release upload, Buffer scheduling |
| `.github/workflows/daily-reel.yml` | Production workflow (one batch + two recoveries) |
| `.github/workflows/tests.yml` | Offline tests, YAML and syntax checks |
| `.github/workflows/keepalive.yml` | Monthly heartbeat commit so GitHub keeps the schedule enabled |
| `setup_buffer.sh` | Buffer CLI preflight; never writes the key to disk |
| `SETUP.md` | One-time setup, rotation checklist, troubleshooting |
| `AUDIT.md` | Evidence-based comparison of the old autopilot and this rewrite |
| `tests/test_pipeline.py` | Offline unit tests |

## References

* MoneyPrinterTurbo v1.3.10: <https://github.com/harry0703/MoneyPrinterTurbo/releases/tag/v1.3.10>
* Buffer API / hosting media: <https://developers.buffer.com/guides/hosting-media.md>
* Buffer CLI: <https://www.npmjs.com/package/@bufferapp/cli>
* Gemini API pricing and free tier: <https://ai.google.dev/gemini-api/docs/pricing>
* Gemini OpenAI compatibility: <https://ai.google.dev/gemini-api/docs/openai>
