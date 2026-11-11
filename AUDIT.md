# Audit: old autopilot vs. this rewrite

Date: 2026-11-11. Scope: `main` at `9395c65`, its last 50 Actions runs, the
`dateflow-media` releases, and the two external tools the pipeline depends on
(`@bufferapp/cli` 2.3.0 and MoneyPrinterTurbo v1.3.10).

No secret values are reproduced here. Credential findings are described by file and by
kind only.

## What was inspected

* Repository files at `main`, not an archive: `pipeline.py`, `.github/workflows/`
  `daily-reel.yml`, `README.md`, `SETUP.md`, `setup_buffer.sh`.
* Actions API: 50 runs, workflow list, per-run jobs and steps.
* Public release assets: name, size, HTTP status, `Content-Type`.
* Buffer CLI schema and help output, run locally with a fake key where read-only.
* MoneyPrinterTurbo v1.3.10 CLI argument parser and `config.example.toml`.

## Findings

| Area | What the repository claimed | What was actually true |
|---|---|---|
| Schedule | README: one run at 10:00 TRT | Four cron jobs at 07:35, 11:35, 14:50, 18:05 UTC |
| Scheduler behaviour | Runs land 25 minutes before each slot | Observed starts were **5-8 hours late**; four scheduled runs per day arrived between 15:45 and 23:09 UTC |
| Result reporting | "Batch complete x/y" Telegram message | `build_and_publish()` ended with `return 0` on every path, including total failure, so every run was green |
| Slot occupancy | Assumed to skip filled slots | Parser looked for statuses `pending`/`failed` and fields `scheduled_at`/`due_at`; Buffer uses `draft`, `error`, `needs_approval`, `scheduled`, `sending`, `sent` and `dueAt` |
| Buffer list fields | None | `posts list` default selection is `items.id`, `items.text`, `items.status`, `pageInfo` - no `dueAt`, so every slot looked free and duplicates were possible |
| Buffer create result | Any `id` in stdout meant success | `createPost` returns a union; success is `PostActionSuccess.post.{id,status}` |
| Script generation | Paid DeepSeek/OpenAI model | One provider, no schema validation, no fallback pool, no CTA guarantee |
| Media hosts | Uguu/Imgur/Litterbox code paths | Those uploaders were dead code; only the GitHub Release path ran |
| Media delivery | Release URL assumed fine | GitHub serves every asset as `application/octet-stream` + `attachment` behind a 302; Buffer asks for a direct public URL, so the URL is verified and `PUBLIC_MEDIA_BASE_URL` exists as an escape hatch |
| MoneyPrinterTurbo | Present but unpinned | `git clone --depth 1` of the default branch; flags and config keys were never checked against a release |
| Buffer CLI | Unpinned | `npm install -g @bufferapp/cli` with no version |
| Manual runs | No inputs | `workflow_dispatch` immediately published with no dry-run mode and no confirmation |
| Tests | None | No syntax, YAML, parser or payload tests |
| Secrets | "Removed" | Current files are clean, but **earlier commits** still contain real-looking API keys, a Telegram bot token, a chat id and organization/channel ids. Removing text does not scrub Git history. |
| Failure signal | Not defined | Real render/Buffer failures produced a successful run, so nobody was alerted |

## Evidence highlights

* All 15 sampled runs had conclusion `success`, including runs that produced no post.
  Job durations of 107 s and 143 s include clone, dependency install, render and upload,
  which is consistent with a single short Reel.
* The public release `dateflow-media` contains only five assets, dated 2026-11-07 to
  2026-11-11, against an expected four per day. New uploads use monthly releases
  (`dateflow-media-YYYY-MM`) so a release never hits GitHub's 1,000-asset limit. There is no trace of most configured slots.
* A manual run on 2026-11-11 created a real Buffer post. The owner's Buffer UI shows one
  Reel in the queue at **18:15** labelled **Custom** (custom schedule), created by the old
  pipeline, with a rendered video thumbnail. Two conclusions: `posts create` does accept
  the GitHub Release URL well enough to build the media preview, and `customScheduled`
  scheduling works end to end. One real publish is still the only proof of publish-time
  fetch.
* The same screenshot shows the old caption contains an emoji and a hook the new curated
  pool does not use, i.e. it came from the unvalidated LLM path. The rewrite rejects
  emoji, markdown, URLs and hashtags inside the script, so this class of caption cannot be
  produced again.
* `posts list` rejects `--json` without `organizationId`; `--json` replaces all individual
  flags. `--fields` and status values were validated against the real CLI.
* Buffer Free allows 10 scheduled posts per channel and **1,000 API requests per channel
  per month**, not an unlimited 3,000. The rewrite skips history and audit calls on empty
  recovery runs to stay near 20 requests per day.

## What the rewrite changes

1. `pipeline.py` is rebuilt around the verified CLI contract: explicit `--fields`, JSON
   filters, real status enums, union-result parsing, and a post-create read-back.
2. Every script path is validated for `hook`, `body`, `cta`, `terms`; a curated 16-script
   pool is the only fallback, so a missing CTA cannot reach Buffer.
3. Slots are planned from TRT slots, never in the past, with a 15-minute tolerance,
   duplicate detection, a queue cap of 10, and a second occupancy check immediately before
   `posts create`.
4. One batch runs at 23:10 TRT and pre-schedules the next TRT day; two recovery crons only
   fill slots that are still in the future. A passed slot is documented as impossible to
   backfill.
5. `workflow_dispatch` defaults to `dry_run`; production from a manual run requires the
   literal `confirm=PUBLISH`.
6. MoneyPrinterTurbo is pinned to v1.3.10 with explicit render flags, `ffprobe`
   verification (1080x1920, H.264, AAC, duration) and the Anton font.
7. Media stays on the public GitHub Release; the URL is fetched with a Range request and
   checked for MP4 bytes. A direct `video/mp4` prefix can be supplied with
   `PUBLIC_MEDIA_BASE_URL`.
8. Empty runs exit 0 and send nothing; real errors exit non-zero and send one summary.
9. `tests.yml` runs 50 offline tests and a dry-run smoke test; it can never publish.
10. `keepalive.yml` writes one timestamp per month so GitHub does not disable the schedule
    after 60 days without repository activity.

## Verified locally

* `pipeline.py` compiles; 55 unit tests pass; shell syntax passes; YAML parses.
* `buffer posts create --input <payload> --dry-run` accepts the payload the pipeline
  builds, including `mode=customScheduled`, `dueAt` with `+03:00`, `assets.video` with
  `metadata.thumbnailOffset`, and `metadata.instagram.type=reel`.
* Buffer field selections used by the pipeline pass the CLI's own validator; a fake key
  produces `AUTH_ERROR` (exit 4), not a usage error.
* MoneyPrinterTurbo v1.3.10 `parse_args` accepts every flag the pipeline passes.

## Credential status (owner decision, 2026-11-11)

The owner confirmed the existing keys are unchanged and already stored as GitHub Actions
secrets, and asked that values never be displayed. They are therefore **still the same
values that exist in this public repository's Git history** and must be treated as
compromised. This project does not rotate them, does not rewrite history and never prints
them; the recommendation to revoke and re-issue stays open in `SETUP.md` section 10.

## Not verified (requires account access or a real run)

* Buffer accepting the GitHub Release URL at publish time. The CLI `--dry-run` never
  fetches media, so this can only be proven by one real post.
* Publish-time behaviour: whether Buffer's server fetch of the Release URL succeeds when
  the post goes live. The queue card rendering the thumbnail is strong but not conclusive
  evidence.
* End-to-end render time on a GitHub runner for four Reels in one job.
