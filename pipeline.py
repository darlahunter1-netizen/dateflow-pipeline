#!/usr/bin/env python3
"""
DateFlow Reels autopilot (MoneyPrinterTurbo edition)
=====================================================

Production runs only inside GitHub Actions. There is no supported local
Windows workflow.

What one production run does
----------------------------
1. Reads the Buffer queue (read-only) to learn which TRT slots are occupied.
2. Picks the next free TRT slots (11:00, 15:00, 18:15, 21:30).
3. Writes an original English short-form script for each slot. The free
   Gemini API is used when GEMINI_API_KEY is present; otherwise a reviewed
   fallback pool is used. No paid API is required.
4. Renders the Reel with MoneyPrinterTurbo (1080x1920, 9:16, word-by-word
   subtitles, Pexels b-roll, transitions incl. zoom, Ken Burns zoom for
   image materials).
5. Uploads the MP4 to the public monthly GitHub Release and verifies
   that the public URL is alive.
6. Schedules the Reel through the official Buffer CLI, then verifies the
   created post's status.
7. Sends at most one Telegram summary. Empty runs send nothing; real failures
   send one message and exit non-zero.

Safety
------
* `--mode dry_run` is the default. It never renders, uploads or publishes.
* A manual production run requires `--confirm PUBLISH` in addition to
  `--mode production`.
* Secrets are read from the environment only and are redacted from errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

import requests

# ── Constants ───────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
TRT = timezone(timedelta(hours=3), "TRT")  # Turkey has used UTC+3 year-round since 2016.

POST_TIMES = ("11:00", "15:00", "18:15", "21:30")
SLOT_TOLERANCE = timedelta(minutes=15)
MIN_LEAD = timedelta(minutes=20)
MAX_QUEUE_DEFAULT = 10
MIN_RENDER_SECONDS = 8
MAX_RENDER_SECONDS = 90

# Real Buffer PostStatus values: draft, error, needs_approval, scheduled,
# sending, sent. "buffer" is accepted by the createPost docs as the queued
# state, so it is treated as occupied too. Unknown states are treated as
# occupied, never as free.
QUEUE_STATUSES = frozenset({"scheduled", "sending", "needs_approval", "buffer"})
OCCUPIED_STATUSES = frozenset(
    {"draft", "error", "needs_approval", "scheduled", "sending", "sent", "buffer"}
)
SUCCESS_POST_STATUSES = frozenset({"scheduled", "buffer"})
BLOCKING_STATUSES = frozenset({"draft", "needs_approval", "error"})

BUFFER_LIST_FIELDS = "items.{id,status,dueAt,channelId,text},pageInfo.{hasNextPage,endCursor}"
BUFFER_CREATE_FIELDS = "post.id,post.status"
BUFFER_GET_FIELDS = "id,status,dueAt,error.message"

CAPTION_HASHTAGS = (
    "#dateideas",
    "#datenight",
    "#firstdate",
    "#datingtips",
    "#dateflow",
    "#datingadvice",
    "#relationshipadvice",
    "#datingcoach",
    "#dating101",
    "#relationshiptips",
    "#moderndating",
    "#datingformen",
)

RELEASE_PREFIX = "dateflow-media"
RELEASE_NAME = "DateFlow Reels Media"
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_LLM_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
TRANSITION_MODE = "shuffle"  # MPT's Shuffle includes fade/slide/zoom transitions.
SUBTITLE_POSITION = "two_thirds_bottom"


# ── Errors and config ───────────────────────────────────────────────────────


class DateFlowError(RuntimeError):
    """Base error with a safe, printable message."""


class ConfigError(DateFlowError):
    pass


class BufferError(DateFlowError):
    def __init__(self, message: str, code: Optional[int] = None, fatal: bool = False):
        super().__init__(message)
        self.code = code
        self.fatal = fatal


class MediaError(DateFlowError):
    pass


class RenderError(DateFlowError):
    pass


@dataclass
class Config:
    mode: str = "dry_run"
    event: str = ""
    target_date: Optional[date] = None
    max_posts: int = 4
    confirm: str = ""
    offline: bool = False
    no_llm: bool = False

    output_dir: Path = field(default_factory=lambda: ROOT / "output")
    mpt_dir: Path = field(default_factory=lambda: Path(os.getenv("MPT_DIR", str(ROOT.parent / "mpt"))))

    buffer_bin: str = ""
    buffer_key: str = field(default="", repr=False)
    buffer_org: str = ""
    buffer_channel: str = ""
    pexels_key: str = field(default="", repr=False)
    gemini_key: str = field(default="", repr=False)
    gemini_base: str = DEFAULT_LLM_BASE
    gemini_model: str = DEFAULT_MODEL
    public_media_base: str = ""
    telegram_token: str = field(default="", repr=False)
    telegram_chat: str = field(default="", repr=False)
    github_token: str = field(default="", repr=False)
    github_repository: str = ""
    github_api: str = "https://api.github.com"
    max_queue: int = MAX_QUEUE_DEFAULT
    replace_errored: bool = True

    @property
    def repository_owner(self) -> str:
        return self.github_repository.split("/", 1)[0] if "/" in self.github_repository else ""

    @property
    def repository_name(self) -> str:
        return self.github_repository.split("/", 1)[1] if "/" in self.github_repository else ""


def load_config(args: argparse.Namespace) -> Config:
    mode = args.mode or os.getenv("MODE", "dry_run")
    if mode not in {"dry_run", "production"}:
        raise ConfigError("mode must be dry_run or production")

    target_date = None
    raw_date = (args.date or os.getenv("TARGET_DATE", "")).strip()
    if raw_date:
        try:
            target_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ConfigError("--date must be YYYY-MM-DD") from exc

    max_posts = args.max_posts
    if max_posts is None:
        max_posts = int(os.getenv("MAX_POSTS", "4"))
    max_posts = max(1, min(4, int(max_posts)))

    return Config(
        mode=mode,
        event=os.getenv("GITHUB_EVENT_NAME", ""),
        target_date=target_date,
        max_posts=max_posts,
        confirm=args.confirm or os.getenv("CONFIRM", ""),
        offline=bool(args.offline),
        no_llm=bool(args.no_llm),
        output_dir=Path(os.getenv("OUTPUT_DIR", str(ROOT / "output"))),
        mpt_dir=Path(os.getenv("MPT_DIR", str(ROOT.parent / "mpt"))),
        buffer_bin=shutil.which(os.getenv("BUFFER_BIN", "buffer")) or "",
        buffer_key=os.getenv("BUFFER_API_KEY", ""),
        buffer_org=os.getenv("BUFFER_ORG_ID", ""),
        buffer_channel=os.getenv("BUFFER_CHANNEL_ID", ""),
        pexels_key=os.getenv("PEXELS_API_KEY", ""),
        gemini_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_base=os.getenv("FREE_LLM_BASE_URL", DEFAULT_LLM_BASE),
        gemini_model=os.getenv("FREE_LLM_MODEL", DEFAULT_MODEL),
        public_media_base=os.getenv("PUBLIC_MEDIA_BASE_URL", "").strip(),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat=os.getenv("ADMIN_CHAT_ID", ""),
        github_token=os.getenv("GITHUB_TOKEN", ""),
        github_repository=os.getenv("GITHUB_REPOSITORY", ""),
        github_api=os.getenv("GITHUB_API_URL", "https://api.github.com"),
        max_queue=max(1, int(os.getenv("BUFFER_MAX_QUEUE", str(MAX_QUEUE_DEFAULT)))),
        replace_errored=os.getenv("REPLACE_ERRORED_SLOTS", "1") not in {"0", "false", "no"},
    )


def _redact(text: str, secrets: Iterable[str]) -> str:
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "[redacted]")
    return out


def redact_for_config(text: str, cfg: Config) -> str:
    return _redact(
        text,
        [cfg.buffer_key, cfg.pexels_key, cfg.gemini_key, cfg.telegram_token, cfg.github_token],
    )


def require_production_config(cfg: Config) -> None:
    if cfg.offline:
        raise ConfigError("--offline cannot be used with production")
    missing = [
        name
        for name, value in (
            ("PEXELS_API_KEY", cfg.pexels_key),
            ("BUFFER_API_KEY", cfg.buffer_key),
            ("BUFFER_ORG_ID", cfg.buffer_org),
            ("BUFFER_CHANNEL_ID", cfg.buffer_channel),
            ("GITHUB_TOKEN", cfg.github_token),
            ("GITHUB_REPOSITORY", cfg.github_repository),
        )
        if not value
    ]
    if missing:
        raise ConfigError("missing required configuration: " + ", ".join(missing))
    if not cfg.buffer_bin:
        raise ConfigError("Buffer CLI is not installed (expected `buffer` on PATH)")
    if not (cfg.mpt_dir / "cli.py").exists():
        raise ConfigError(f"MoneyPrinterTurbo not found at MPT_DIR ({cfg.mpt_dir})")


# ── Time and slot planning ──────────────────────────────────────────────────


def trt_now() -> datetime:
    return datetime.now(UTC).astimezone(TRT)


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # Python 3.11 accepts at most microseconds; trim longer fractions.
        text = re.sub(r"(\.\d{6})\d+", r"\1", text)
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def slot_datetime(day: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TRT)


def slot_label(slot: datetime) -> str:
    return slot.astimezone(TRT).strftime("%a %d %b %H:%M TRT")


@dataclass(frozen=True)
class ExistingPost:
    id: str
    status: str
    due_at: Optional[datetime]
    text: str = ""

    def matches_slot(self, slot: datetime, tolerance: timedelta = SLOT_TOLERANCE) -> bool:
        if self.due_at is None:
            return False
        return abs((self.due_at - slot.astimezone(UTC)).total_seconds()) <= tolerance.total_seconds()


@dataclass
class Plan:
    slots: list[datetime] = field(default_factory=list)
    blocked: list[ExistingPost] = field(default_factory=list)
    reason: str = ""
    queue_count: int = 0


def plan_slots(
    now: datetime,
    existing: Iterable[ExistingPost],
    target_day: Optional[date] = None,
    max_posts: int = 4,
    max_queue: int = MAX_QUEUE_DEFAULT,
    min_lead: timedelta = MIN_LEAD,
) -> Plan:
    """Return the next free slots without ever returning a past slot.

    * An explicit target_day only plans that TRT day.
    * Without target_day, today's remaining slots are used. The planner rolls
      to tomorrow only when today's last slot has already passed (for example
      the 23:10 TRT daily batch). This avoids a recovery run at 06:00 TRT
      scheduling tomorrow's posts a day early.
    """
    now = now.astimezone(UTC)
    posts = list(existing)
    queue_count = sum(
        1
        for post in posts
        if post.status in QUEUE_STATUSES and post.due_at and post.due_at >= now - timedelta(days=1)
    )
    capacity = max(0, max_queue - queue_count)

    days: list[date] = []
    if target_day is not None:
        days = [target_day]
    else:
        days = [now.astimezone(TRT).date()]
        last_slot_today = slot_datetime(days[0], POST_TIMES[-1])
        # Once the last slot is inside the lead window it can no longer be
        # planned, so the next TRT day becomes eligible as well.
        if now >= (last_slot_today - min_lead).astimezone(UTC):
            days.append(days[0] + timedelta(days=1))

    slots: list[datetime] = []
    blocked: list[ExistingPost] = []
    reason = ""

    for day in days:
        day_slots = [slot_datetime(day, hhmm) for hhmm in POST_TIMES]
        for slot in day_slots:
            if slot.astimezone(UTC) - now < min_lead:
                continue
            matches = [p for p in posts if p.matches_slot(slot)]
            if matches:
                for post in matches:
                    if post.status in BLOCKING_STATUSES and post not in blocked:
                        blocked.append(post)
                continue
            if len(slots) >= min(max_posts, capacity):
                if capacity < max_posts:
                    reason = "queue_capacity"
                break
            slots.append(slot)
        if target_day is not None or slots:
            break
        if len(slots) >= min(max_posts, capacity):
            break

    if not slots and not blocked and capacity == 0:
        reason = "queue_capacity"
    elif not slots and not blocked and not reason:
        reason = "no_future_slots"

    return Plan(slots=slots, blocked=blocked, reason=reason, queue_count=queue_count)


# ── Buffer CLI ──────────────────────────────────────────────────────────────


def _run_buffer(cfg: Config, args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    if not cfg.buffer_bin:
        raise BufferError("Buffer CLI is not installed", fatal=True)
    env = os.environ.copy()
    env["BUFFER_API_KEY"] = cfg.buffer_key
    env.setdefault("NO_COLOR", "1")
    try:
        return subprocess.run(
            [cfg.buffer_bin, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise BufferError("Buffer CLI timed out", fatal=False) from exc
    except OSError as exc:
        raise BufferError(f"could not start Buffer CLI: {exc}", fatal=True) from exc


def _buffer_error_from_output(result: subprocess.CompletedProcess) -> BufferError:
    raw = (result.stderr or result.stdout or "").strip()
    code: Optional[int] = None
    message = ""
    try:
        payload = json.loads(raw)
        message = str(payload.get("message") or payload.get("error") or "").strip()
        code = int(payload.get("code")) if payload.get("code") is not None else None
    except (json.JSONDecodeError, TypeError, ValueError):
        message = raw.splitlines()[0][:300] if raw else ""
    if result.returncode == 4:
        code = code or 4
    fatal = result.returncode in {2, 4}
    return BufferError(
        f"Buffer CLI failed (exit {result.returncode}{f', code {code}' if code else ''})"
        + (f": {message}" if message else ""),
        code=code,
        fatal=fatal,
    )


def buffer_list_posts(
    cfg: Config,
    start: datetime,
    end: datetime,
    *,
    statuses: Optional[Iterable[str]] = None,
    limit: int = 100,
    purpose: str = "future",
    allow_truncation: bool = False,
) -> list[ExistingPost]:
    # The default (None) omits the status filter on purpose: Buffer's status
    # enum has changed over time and an unlisted status such as the legacy
    # "buffer" value must still be visible for duplicate detection.
    filters: dict[str, Any] = {
        "channelIds": [cfg.buffer_channel],
        "dueAt": {
            "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    if statuses is not None:
        filters["status"] = list(statuses)
    payload = {"organizationId": cfg.buffer_org, "filter": filters}
    args = [
        "posts",
        "list",
        "--json",
        json.dumps(payload, ensure_ascii=False),
        "--fields",
        BUFFER_LIST_FIELDS,
        "--limit",
        str(limit),
        "--output",
        "json",
    ]
    result = _run_buffer(cfg, args, timeout=90)
    if result.returncode != 0:
        raise _buffer_error_from_output(result)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BufferError("Buffer posts list did not return JSON", fatal=False) from exc
    if isinstance(data, dict) and data.get("error"):
        raise BufferError(f"Buffer posts list error: {data.get('error')}", fatal=False)

    items: Any = []
    page_info: dict[str, Any] = {}
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items") or []
        page_info = data.get("pageInfo") or {}
        if not items and isinstance(data.get("data"), dict):
            items = data["data"].get("items") or []
            page_info = page_info or data["data"].get("pageInfo") or {}
    if not isinstance(items, list):
        raise BufferError("Buffer posts list returned an unexpected shape", fatal=False)

    posts: list[ExistingPost] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        due = parse_datetime(item.get("dueAt") or item.get("due_at"))
        status = str(item.get("status") or "").lower()
        posts.append(
            ExistingPost(
                id=str(item.get("id") or ""),
                status=status,
                due_at=due,
                text=str(item.get("text") or ""),
            )
        )

    if page_info.get("hasNextPage"):
        if allow_truncation:
            print(f"  note: Buffer {purpose} history truncated at {len(posts)} posts")
        else:
            raise BufferError(
                f"Buffer {purpose} list is paginated ({len(posts)} posts on page 1); refusing to write without a full view",
                fatal=False,
            )
    return posts


def buffer_post_get(cfg: Config, post_id: str) -> dict[str, Any]:
    result = _run_buffer(
        cfg,
        ["posts", "get", "--id", post_id, "--fields", BUFFER_GET_FIELDS, "--output", "json"],
        timeout=60,
    )
    if result.returncode != 0:
        raise _buffer_error_from_output(result)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BufferError("Buffer posts get did not return JSON", fatal=False) from exc
    post = _extract_post(data)
    if not post:
        raise BufferError("Buffer posts get did not return a post", fatal=False)
    return post


def _extract_post(data: Any) -> Optional[dict[str, Any]]:
    if isinstance(data, dict):
        post = data.get("post")
        if isinstance(post, dict) and post.get("id"):
            return post
        if data.get("id") and "status" in data:
            return data
        for value in data.values():
            found = _extract_post(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _extract_post(value)
            if found:
                return found
    return None


def buffer_create_post(cfg: Config, payload: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        path = handle.name
    try:
        args = ["posts", "create", "--input", path]
        if dry_run:
            args.append("--dry-run")
        else:
            args.extend(["--fields", BUFFER_CREATE_FIELDS])
        args.extend(["--output", "json"])
        result = _run_buffer(cfg, args, timeout=180)
        if result.returncode != 0:
            raise _buffer_error_from_output(result)
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise BufferError("Buffer posts create did not return JSON", fatal=False) from exc
        if dry_run:
            if isinstance(data, dict) and data.get("error"):
                raise BufferError(f"Buffer payload validation failed: {data.get('error')}", fatal=True)
            return data if isinstance(data, dict) else {}
        if isinstance(data, dict) and data.get("error") and not data.get("post"):
            raise BufferError(f"Buffer create error: {data.get('error')}", fatal=False)
        post = _extract_post(data)
        if not post:
            message = ""
            if isinstance(data, dict):
                message = str(data.get("message") or data.get("error") or "").strip()
            raise BufferError(
                "Buffer did not confirm a created post"
                + (f": {message[:200]}" if message else ""),
                fatal=False,
            )
        return post
    finally:
        Path(path).unlink(missing_ok=True)


def buffer_delete_post(cfg: Config, post_id: str) -> None:
    result = _run_buffer(cfg, ["posts", "delete", "--id", post_id, "--output", "json"], timeout=60)
    if result.returncode == 0:
        return
    err = _buffer_error_from_output(result)
    if "not found" in str(err).lower():
        return
    raise err


def buffer_validate_payload(cfg: Config, payload: dict[str, Any]) -> None:
    """Use the real CLI local validator. No API call is made."""
    buffer_create_post(cfg, payload, dry_run=True)
    print("  Buffer payload validated locally with `posts create --dry-run`")


# ── Scripts ─────────────────────────────────────────────────────────────────

# Topics are deliberately broad and non-repetitive. The model output is still
# validated, so a bad topic cannot become a bad post.
TOPICS = (
    "first date planning that lowers pressure",
    "questions that create a real conversation",
    "consent and reading a clear yes",
    "accepting a no with grace",
    "cheap date ideas that feel special",
    "texting with clarity instead of games",
    "planning together instead of controlling the date",
    "listening as the rarest date skill",
    "dating profile specifics that build trust",
    "second date pacing",
    "nerves versus red flags",
    "honest follow-up after a good date",
    "alcohol and first date safety",
    "small respectful physical check-ins",
    "making time for a date without vague maybe",
    "repairing a small mistake on a date",
)

UNSAFE_PATTERNS = re.compile(
    r"\b("
    r"ignore (her|his|their|the) no|don'?t take no|pressure (her|him|them)|"
    r"get (her|him|them) drunk|negs?|negging|manipulat\w*|gaslight\w*|"
    r"trick (her|him|them)|make (her|him|them) jealous|love ?bomb\w*|"
    r"stalk\w*|persist until|wear (her|him|them) down|"
    r"all women|all men|females|males are"
    r")\b",
    re.IGNORECASE,
)
BAD_STYLE_PATTERNS = re.compile(
    r"(?:https?://|www\.|#\w|```|\*\*|__|[\U0001F300-\U0001FAFF\u2600-\u27BF])",
    re.UNICODE,
)


# This pool is the reviewed fallback when the free LLM is unavailable or
# returns invalid JSON. Every entry contains hook/body/cta/terms.
FALLBACK_SCRIPTS: tuple[dict[str, str], ...] = (
    {
        "hook": "The best first date is shorter than you think",
        "body": "Plan one hour, not five. Coffee, then a walk if it is going well. A short date protects both calendars and makes the next one something you both want. Ending on a high note beats forcing a marathon.",
        "cta": "DateFlow builds the route in minutes.",
        "terms": "couple coffee shop window, couple walking city street, first date coffee, urban park bench couple, evening city walk",
    },
    {
        "hook": "Stop asking what do you do for work",
        "body": "That question turns a date into an interview. Ask what they have been into lately, then follow the answer with a real follow-up. People remember how curious you made them feel, not your list of questions.",
        "cta": "One good route. Zero awkward silences.",
        "terms": "couple talking cafe close up, active listening date, couple laughing conversation, coffee date hands, city cafe window couple",
    },
    {
        "hook": "Consent is not a mood killer",
        "body": "Check in before you close the distance. A simple is this okay, said warmly, shows you are paying attention. Enthusiastic yes is the only green light. A pause or a no deserves a smile and a step back.",
        "cta": "Better dates start with respect.",
        "terms": "couple respectful conversation, couple eye contact close, romantic couple talking sofa, gentle couple moment, honest conversation date",
    },
    {
        "hook": "Your date does not owe you a second one",
        "body": "If they say no, thank them and mean it. Do not negotiate, sulk, or ask why. A graceful exit protects your reputation and leaves the door open for a genuine yes with someone else.",
        "cta": "DateFlow plans the next good one.",
        "terms": "person walking away city, confident person smiling alone, cafe exit daylight, calm person street, sunrise city solo",
    },
    {
        "hook": "The cheapest date can be the most memorable",
        "body": "Skip the expensive dinner. Pick a market, split something you have never tried, and walk it off. Novelty does the work that money usually gets blamed for. You leave with a story instead of a bill.",
        "cta": "We map low-key spots that feel special.",
        "terms": "street food market couple, couple sharing food, city market walk, sunset bridge couple, casual date walking",
    },
    {
        "hook": "Do not text back just to win",
        "body": "Waiting three hours to look busy is a game nobody enjoys. Reply when you see it, say something specific, and make a plan. Calm consistency is more attractive than a perfectly timed message.",
        "cta": "Less games. More good dates.",
        "terms": "person texting phone cafe, phone screen message close, couple texting smiling, morning coffee phone, city bench phone",
    },
    {
        "hook": "Planning everything is not romantic",
        "body": "Offer two good options and let them choose. You show initiative without taking over. The date feels like something you built together, not a schedule they were handed. It also makes the next decision easier.",
        "cta": "Two options, one great night.",
        "terms": "couple choosing menu, couple planning map, two coffee cups table, couple decision smiling, notebook cafe couple",
    },
    {
        "hook": "Listen longer than feels comfortable",
        "body": "Most people fill silence with another question. Instead, repeat the last thing they said and ask what happened next. Being heard is rarer than being impressive, and it costs nothing.",
        "cta": "DateFlow handles the plan. You handle the presence.",
        "terms": "couple listening close, woman talking man listening, cafe conversation warm, couple eye contact table, quiet restaurant couple",
    },
    {
        "hook": "Your profile does not need ten photos",
        "body": "Three clear photos beat a highlight reel. One face, one full body, one thing you actually do. Write two specific lines about how you spend a free Saturday. Specific is believable; vague is forgettable.",
        "cta": "Build the profile, then build the date.",
        "terms": "person taking selfie window, phone profile close up, city weekend hobby, casual portrait street, coffee and book table",
    },
    {
        "hook": "A second date should be even shorter",
        "body": "Pick one thing you both missed the first time. Ninety minutes, no pressure, an easy goodbye. The goal is not to exhaust the conversation. It is to leave enough for a third.",
        "cta": "DateFlow keeps the next plan easy.",
        "terms": "couple dessert cafe, short walk evening couple, couple saying goodbye doorway, small bar couple, city lights couple",
    },
    {
        "hook": "Nerves are not a red flag",
        "body": "A shaking hand can mean someone cares. Judge how they treat the waiter, whether they listen, and how they respond to a boundary. Calm is nice; respect is the signal.",
        "cta": "Choose people who make respect feel normal.",
        "terms": "couple nervous smile, hands fidgeting cafe, couple polite waiter, calm couple talking, first date table",
    },
    {
        "hook": "The 24-hour text rule is bad advice",
        "body": "Message when you get home if you had a good time. Mention one specific moment. No essay, no scorekeeping. Honest enthusiasm filters for people who want the same thing.",
        "cta": "Say what you mean. Then plan the next one.",
        "terms": "person texting evening window, phone glow face, couple text smile, city night phone, cozy room texting",
    },
    {
        "hook": "Do not plan a date around getting drunk",
        "body": "Alcohol can hide a bad fit, not fix one. Pick a place with good non-alcoholic options and keep the first round slow. You want to remember the person, not the tab.",
        "cta": "Plan a date you will both remember.",
        "terms": "mocktail bar couple, couple toast drinks, bartender alcohol free, cafe evening couple, sober date conversation",
    },
    {
        "hook": "Ask before you touch",
        "body": "A hand on the back can be welcome or invasive. Ask, or move slowly and watch for a clear yes. Small check-ins build trust and make real chemistry easier to notice.",
        "cta": "Respect is the baseline, not the bonus.",
        "terms": "couple holding hands, couple close conversation, gentle touch shoulder, respectful couple walk, warm couple portrait",
    },
    {
        "hook": "A busy schedule is not a status symbol",
        "body": "If you want to meet, offer a real window. Maybe Tuesday at seven. Vague maybe later reads as lukewarm. Clear plans respect their time and yours.",
        "cta": "Pick a time. We will pick the place.",
        "terms": "calendar planner coffee, couple scheduling phone, city cafe table, person writing planner, clock window cafe",
    },
    {
        "hook": "The best apology on a date is a small one",
        "body": "If you are late, say sorry once, then change the subject and be present. Over-explaining makes the date about your guilt. Repair quickly, then get back to the person in front of you.",
        "cta": "Good dates leave room to be human.",
        "terms": "person arriving cafe late, couple greeting table, watch wrist coffee, couple relaxed talk, city door cafe",
    },
)


def normalize_hook(hook: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", hook.lower()).strip()


def hook_is_similar(hook: str, avoid_hooks: Iterable[str], threshold: float = 0.86) -> bool:
    key = normalize_hook(hook)
    if not key:
        return True
    for other in avoid_hooks:
        other_key = normalize_hook(other)
        if not other_key:
            continue
        if key == other_key:
            return True
        if SequenceMatcher(None, key, other_key).ratio() >= threshold:
            return True
    return False


def parse_script_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("response did not contain a JSON object")


def normalize_script(script: dict[str, Any]) -> dict[str, str]:
    if not isinstance(script, dict):
        raise ValueError("script must be a JSON object")
    missing = [key for key in ("hook", "body", "cta", "terms") if not script.get(key)]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))

    terms_value = script["terms"]
    if isinstance(terms_value, (list, tuple)):
        terms = ", ".join(str(item).strip() for item in terms_value if str(item).strip())
    else:
        terms = str(terms_value)
    terms = re.sub(r"\s*,\s*", ", ", terms).strip()

    return {
        "hook": re.sub(r"\s+", " ", str(script["hook"])).strip(),
        "body": re.sub(r"\s+", " ", str(script["body"])).strip(),
        "cta": re.sub(r"\s+", " ", str(script["cta"])).strip(),
        "terms": terms,
    }


def validate_script(script: dict[str, Any], avoid_hooks: Iterable[str] = ()) -> tuple[bool, str]:
    try:
        s = normalize_script(script)
    except ValueError as exc:
        return False, str(exc)

    joined = " ".join((s["hook"], s["body"], s["cta"]))
    if BAD_STYLE_PATTERNS.search(joined):
        return False, "script contains markdown, a URL, a hashtag or an emoji"
    if UNSAFE_PATTERNS.search(joined):
        return False, "script contains coercive or disrespectful language"
    if re.search(r"\b(guarantee|guaranteed|always works|never fails)\b", joined, re.IGNORECASE):
        return False, "script makes a guarantee"

    hook_words = len(s["hook"].split())
    body_words = len(s["body"].split())
    cta_words = len(s["cta"].split())
    total_words = hook_words + body_words + cta_words

    if not 3 <= hook_words <= 12:
        return False, "hook must be 3-12 words"
    if not 20 <= body_words <= 60:
        return False, "body must be 20-60 words"
    if not 2 <= cta_words <= 12:
        return False, "cta must be 2-12 words"
    if not 40 <= total_words <= 85:
        return False, "script must be 40-85 words in total"

    if hook_is_similar(s["hook"], avoid_hooks):
        return False, "hook is too similar to an existing hook"

    terms = [part.strip() for part in s["terms"].split(",") if part.strip()]
    if not 3 <= len(terms) <= 6:
        return False, "terms must contain 3-6 comma-separated phrases"
    for term in terms:
        if not 1 <= len(term.split()) <= 5:
            return False, "each Pexels term must be 1-5 words"
        if BAD_STYLE_PATTERNS.search(term):
            return False, "Pexels terms must not contain markdown, URLs, hashtags or emoji"
    return True, ""


def _choose_topic(avoid_hooks: Iterable[str]) -> str:
    used = " ".join(avoid_hooks).lower()
    fresh = [topic for topic in TOPICS if not any(word in used for word in topic.split()[:2])]
    return random.choice(fresh or list(TOPICS))


def generate_script_llm(cfg: Config, avoid_hooks: Iterable[str]) -> Optional[dict[str, str]]:
    if cfg.offline or cfg.no_llm or not cfg.gemini_key:
        return None
    avoid = list(avoid_hooks)[:30]
    avoid_text = "\n".join(f"- {hook}" for hook in avoid) or "- (none)"
    system_prompt = (
        "You are an original short-form scriptwriter for an English-language dating-advice "
        "Instagram account for adults 22-35. Voice: calm, specific, emotionally intelligent, "
        "lightly contrarian. Never copy or paraphrase a known creator. Never suggest "
        "manipulation, pressure, coercion, jealousy games, negging, persistence after a no, "
        "or getting someone drunk. Consent and respect are non-negotiable. No gendered "
        "stereotypes, no guarantees, no medical or legal claims.\n\n"
        "Write ONE 18-24 second Reel script in US English with this structure:\n"
        "- hook: 3-10 words, a concrete tension or unexpected claim that lands in the first "
        "1-2 seconds. Avoid question cliches and 'here is why'.\n"
        "- body: 22-55 words, 2-3 short sentences, ONE practical tip with a specific example, "
        "then a small payoff or reframe.\n"
        "- cta: 3-10 words, low-pressure, about planning a better date with DateFlow. No "
        "download-now pressure.\n"
        "- terms: 3-6 comma-free Pexels search phrases (2-4 words each), portrait-friendly, "
        "no brand names, no text overlays.\n\n"
        "Return ONLY a JSON object with exactly these keys: hook, body, cta, terms (terms as "
        "one comma-separated string). No markdown, no emojis, no hashtags, no URLs."
    )
    topic = _choose_topic(avoid)
    user_prompt = (
        f"Topic: {topic}.\n\nDo not reuse these existing hooks or close variants:\n{avoid_text}\n\n"
        "Make the hook feel like a real observation, not a template."
    )
    body = {
        "model": cfg.gemini_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 1.0,
        "top_p": 0.95,
        "max_tokens": 700,
    }
    url = cfg.gemini_base.rstrip("/") + "/chat/completions"
    for attempt in range(2):
        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {cfg.gemini_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=60,
            )
        except requests.RequestException as exc:
            print(f"  free LLM request failed: {type(exc).__name__}")
            if attempt == 0:
                time.sleep(3)
                continue
            return None
        if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
            print(f"  free LLM temporary status {response.status_code}; retrying once")
            time.sleep(4)
            continue
        if response.status_code != 200:
            print(f"  free LLM unavailable (HTTP {response.status_code}); using fallback pool")
            return None
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            return normalize_script(parse_script_json(str(content)))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            print(f"  free LLM returned invalid JSON ({type(exc).__name__}); using fallback pool")
            return None
    return None


def pick_script(cfg: Config, avoid_hooks: Iterable[str]) -> dict[str, str]:
    avoid = list(avoid_hooks)
    for _ in range(2):
        candidate = generate_script_llm(cfg, avoid)
        if not candidate:
            break
        ok, reason = validate_script(candidate, avoid)
        if ok:
            return normalize_script(candidate)
        print(f"  rejected LLM script: {reason}")
        avoid.append(candidate.get("hook", ""))

    pool = [normalize_script(item) for item in FALLBACK_SCRIPTS]
    unused = [item for item in pool if not hook_is_similar(item["hook"], avoid)]
    reuse = not unused
    if reuse:
        print("  note: all fallback hooks have been used recently; allowing a controlled reuse")
        unused = pool
    chosen = random.choice(unused)
    ok, reason = validate_script(chosen, [] if reuse else avoid)
    if not ok:
        raise DateFlowError(f"curated fallback script failed validation: {reason}")
    return chosen


def caption_for(script: dict[str, str]) -> str:
    seed = int(hashlib.sha256(normalize_hook(script["hook"]).encode("utf-8")).hexdigest()[:8], 16)
    tags = random.Random(seed).sample(CAPTION_HASHTAGS, 6)
    caption = f"{script['hook']}\n\n{script['body']}\n\n{script['cta']}\n\n{' '.join(tags)}"
    if len(caption) > 2200:
        raise DateFlowError("caption is longer than Instagram's 2200-character limit")
    return caption


def full_script_text(script: dict[str, str]) -> str:
    hook = script["hook"].rstrip()
    if not hook.endswith((".", "!", "?", ":")):
        hook += "."
    return f"{hook} {script['body']} {script['cta']}".strip()


# ── Buffer payload ──────────────────────────────────────────────────────────


def build_buffer_payload(cfg: Config, script: dict[str, str], slot: datetime, media_url: str) -> dict[str, Any]:
    # Last line of defence: even a fallback pool entry must carry all four
    # fields before it can reach Buffer, so a missing "cta" is impossible here.
    try:
        script = normalize_script(script)
    except ValueError as exc:
        raise DateFlowError(f"script is missing required fields: {exc}") from exc
    ok, reason = validate_script(script)
    if not ok:
        raise DateFlowError(f"script failed validation: {reason}")
    if not media_url.startswith("https://"):
        raise DateFlowError("media URL must use HTTPS")
    due_at = slot.astimezone(TRT).isoformat()
    return {
        "channelId": cfg.buffer_channel or "dry-run-channel",
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": due_at,
        "text": caption_for(script),
        "assets": [
            {
                "video": {
                    "url": media_url,
                    "metadata": {"title": "DateFlow Reel", "thumbnailOffset": 2000},
                }
            }
        ],
        "metadata": {"instagram": {"type": "reel", "shouldShareToFeed": True}},
        "aiAssisted": True,
        "source": "dateflow-pipeline",
    }


def validate_payload_shape(payload: dict[str, Any]) -> None:
    if not payload.get("channelId"):
        raise DateFlowError("payload is missing channelId")
    if payload.get("schedulingType") != "automatic":
        raise DateFlowError("payload must use schedulingType=automatic")
    if payload.get("mode") != "customScheduled":
        raise DateFlowError("payload must use mode=customScheduled")
    due = parse_datetime(payload.get("dueAt"))
    if not due:
        raise DateFlowError("payload dueAt must be an ISO-8601 timestamp with an offset")
    text = str(payload.get("text") or "")
    if not text or len(text) > 2200:
        raise DateFlowError("payload text must be 1-2200 characters")
    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets:
        raise DateFlowError("payload must contain one video asset")
    video = (assets[0] or {}).get("video") or {}
    if not str(video.get("url", "")).startswith("https://"):
        raise DateFlowError("payload video URL must use HTTPS")
    instagram = ((payload.get("metadata") or {}).get("instagram") or {})
    if instagram.get("type") != "reel":
        raise DateFlowError("payload must set metadata.instagram.type=reel")
    if instagram.get("shouldShareToFeed") is not True:
        raise DateFlowError("payload must set metadata.instagram.shouldShareToFeed=true")
    offset = (video.get("metadata") or {}).get("thumbnailOffset")
    if not isinstance(offset, int) or offset < 0:
        raise DateFlowError("payload must set a non-negative integer thumbnailOffset")


# ── MoneyPrinterTurbo ───────────────────────────────────────────────────────


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def ensure_mpt_config(cfg: Config) -> None:
    if not cfg.pexels_key:
        raise ConfigError("PEXELS_API_KEY is required to render with MoneyPrinterTurbo")
    mpt = cfg.mpt_dir
    if not (mpt / "cli.py").exists():
        raise ConfigError(f"MoneyPrinterTurbo not found at MPT_DIR ({mpt})")
    config = f"""log_level = "WARNING"
listen_host = "127.0.0.1"
listen_port = 8080

[app]
video_source = "pexels"
pexels_api_keys = [{_toml_string(cfg.pexels_key)}]
video_codec = "libx264"

[whisper]
model_size = "base"
device = "cpu"
compute_type = "int8"

[ui]
subtitle_enabled = true
subtitle_display_mode = "word_by_word"
subtitle_animation = "pop_spring"
subtitle_position = "{SUBTITLE_POSITION}"
font_name = "Anton-Regular.ttf"
font_size = 78
text_fore_color = "#FFFFFF"
stroke_color = "#000000"
stroke_width = 3.0
subtitle_background_enabled = false
video_transition_mode = "Shuffle"
video_fit_mode = "cover"
bgm_type = "none"
video_aspect_pexels = "9:16"
video_clip_duration = 3
"""
    (mpt / "config.toml").write_text(config, encoding="utf-8")
    _ensure_anton_font(mpt)


def _ensure_anton_font(mpt: Path) -> None:
    fonts = mpt / "resource" / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    target = fonts / "Anton-Regular.ttf"
    if target.exists() and target.stat().st_size > 10_000:
        return
    url = "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"
    try:
        response = requests.get(url, timeout=45)
        response.raise_for_status()
        if len(response.content) < 10_000:
            raise MediaError("Anton font download was too small")
        target.write_bytes(response.content)
        return
    except Exception as exc:
        print(f"  warning: could not download Anton font ({type(exc).__name__}); copying DejaVu Bold")
    fallback = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if fallback.exists():
        shutil.copy(fallback, target)
    else:
        print("  warning: no fallback font found; MPT will use its default")


def _ffprobe_info(path: Path) -> Optional[dict[str, Any]]:
    if not shutil.which("ffprobe"):
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RenderError("ffprobe could not read the rendered video")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "codec": video.get("codec_name") or "",
        "audio_codec": audio.get("codec_name") or "",
        "duration": float((data.get("format") or {}).get("duration") or 0),
    }


def verify_video(path: Path) -> None:
    if not path.exists() or path.stat().st_size < 500_000:
        raise RenderError("rendered video is missing or suspiciously small")
    info = _ffprobe_info(path)
    if info is None:
        print("  warning: ffprobe is unavailable; skipping the 1080x1920 check")
        return
    if (info["width"], info["height"]) != (1080, 1920):
        raise RenderError(f"rendered video is {info['width']}x{info['height']}, expected 1080x1920")
    if info["codec"] not in {"h264", "avc1"}:
        raise RenderError(f"rendered video codec is {info['codec']!r}, expected h264")
    if info["audio_codec"] not in {"aac", "mp3"}:
        raise RenderError(
            f"rendered video audio codec is {info['audio_codec'] or 'missing'!r}, expected AAC"
        )
    if not MIN_RENDER_SECONDS <= info["duration"] <= MAX_RENDER_SECONDS:
        raise RenderError(f"rendered video duration is {info['duration']:.1f}s, outside the allowed range")


def render_with_mpt(cfg: Config, script: dict[str, str]) -> Path:
    ensure_mpt_config(cfg)
    task_id = str(uuid.uuid4())
    command = [
        sys.executable,
        "cli.py",
        "--video-script",
        full_script_text(script),
        "--video-terms",
        script["terms"],
        "--video-language",
        "en-US",
        "--video-aspect",
        "9:16",
        "--video-source",
        "pexels",
        "--video-fit-mode",
        "cover",
        "--image-motion",
        "zoom",
        "--video-transition-mode",
        TRANSITION_MODE,
        "--video-clip-duration",
        "3",
        "--voice-name",
        "en-US-GuyNeural",
        "--voice-rate",
        "1.1",
        "--subtitle-enabled",
        "--subtitle-display-mode",
        "word_by_word",
        "--subtitle-animation",
        "pop_spring",
        "--subtitle-position",
        SUBTITLE_POSITION,
        "--font-name",
        "Anton-Regular.ttf",
        "--font-size",
        "78",
        "--text-fore-color",
        "#FFFFFF",
        "--stroke-color",
        "#000000",
        "--stroke-width",
        "3.0",
        "--bgm-type",
        "none",
        "--n-threads",
        "4",
        "--task-id",
        task_id,
    ]
    print(f"  rendering with MoneyPrinterTurbo (task {task_id[:8]}...)")
    started = time.time()
    try:
        result = subprocess.run(
            command,
            cwd=str(cfg.mpt_dir),
            capture_output=True,
            text=True,
            timeout=1200,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError("MoneyPrinterTurbo render timed out") from exc

    stdout_lines = [line for line in (result.stdout or "").splitlines() if line.strip().startswith("{")]
    payload: Optional[dict[str, Any]] = None
    for line in reversed(stdout_lines):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        tail = (result.stderr or result.stdout or "")[-400:].replace("\n", " ")
        raise RenderError(f"MoneyPrinterTurbo returned no JSON result: {tail}")

    task_result = payload.get("result") or {}
    if not isinstance(task_result, dict):
        raise RenderError("MoneyPrinterTurbo returned an unexpected result")
    if str(task_result.get("state", "")).lower() == "failed" or task_result.get("error"):
        stage = task_result.get("failed_stage") or "unknown"
        error = str(task_result.get("error") or "unknown error")[:300]
        raise RenderError(f"MoneyPrinterTurbo failed at {stage}: {error}")

    videos = task_result.get("videos") or []
    if not videos:
        raise RenderError("MoneyPrinterTurbo finished without a video path")
    video_path = Path(str(videos[0]))
    if not video_path.is_absolute():
        candidate = cfg.mpt_dir / video_path
        if candidate.exists():
            video_path = candidate
    if not video_path.exists():
        raise RenderError("MoneyPrinterTurbo reported a video path that does not exist")
    verify_video(video_path)
    print(f"  render complete in {time.time() - started:.0f}s ({video_path.stat().st_size / 1_048_576:.1f} MB)")
    return video_path


# ── GitHub Release ──────────────────────────────────────────────────────────


class ReleaseUploader:
    def __init__(self, cfg: Config):
        if not cfg.github_token or not cfg.github_repository:
            raise MediaError("GITHUB_TOKEN and GITHUB_REPOSITORY are required for release uploads")
        self.cfg = cfg
        self.api = cfg.github_api.rstrip("/") + f"/repos/{cfg.github_repository}"
        self.headers = {
            "Authorization": f"Bearer {cfg.github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "DateFlow-Pipeline",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _release(self, tag: str) -> dict[str, Any]:
        response = requests.get(
            f"{self.api}/releases/tags/{tag}", headers=self.headers, timeout=60
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code != 404:
            raise MediaError(f"GitHub release lookup failed with HTTP {response.status_code}")

        body = {
            "tag_name": tag,
            "name": f"{RELEASE_NAME} {tag[-7:]}",
            "body": "Auto-generated DateFlow Reels. Public assets for Buffer scheduling.",
            "draft": False,
            "prerelease": False,
            "target_commitish": "main",
        }
        created = requests.post(f"{self.api}/releases", headers=self.headers, json=body, timeout=90)
        if created.status_code in {200, 201}:
            return created.json()
        if created.status_code == 422:
            # The tag may exist without a release. Point it at the current commit
            # if needed, then retry once.
            sha = os.getenv("GITHUB_SHA", "")
            if sha:
                requests.post(
                    f"{self.api}/git/refs",
                    headers=self.headers,
                    json={"ref": f"refs/tags/{tag}", "sha": sha},
                    timeout=60,
                )
            created = requests.post(f"{self.api}/releases", headers=self.headers, json=body, timeout=90)
            if created.status_code in {200, 201}:
                return created.json()
            response = requests.get(
                f"{self.api}/releases/tags/{tag}", headers=self.headers, timeout=60
            )
            if response.status_code == 200:
                return response.json()
        raise MediaError(f"GitHub release creation failed with HTTP {created.status_code}")

    def _find_asset(self, release: dict[str, Any], name: str) -> Optional[dict[str, Any]]:
        response = requests.get(
            f"{self.api}/releases/{release['id']}/assets?per_page=100",
            headers=self.headers,
            timeout=60,
        )
        if response.status_code != 200:
            return None
        for asset in response.json():
            if asset.get("name") == name:
                return asset
        return None

    def upload(self, path: Path, tag: str) -> str:
        release = self._release(tag)
        upload_url = release["upload_url"].split("{", 1)[0]
        name = path.name

        for attempt in range(3):
            with open(path, "rb") as handle:
                response = requests.post(
                    f"{upload_url}?name={quote(name)}",
                    headers={**self.headers, "Content-Type": "video/mp4"},
                    data=handle,
                    timeout=900,
                )
            if response.status_code in {200, 201}:
                asset = response.json()
                if asset.get("state") != "uploaded":
                    raise MediaError("GitHub asset did not reach the uploaded state")
                if int(asset.get("size") or 0) != path.stat().st_size:
                    raise MediaError("GitHub asset size does not match the rendered file")
                return str(asset["browser_download_url"])

            if response.status_code == 422:
                existing = self._find_asset(release, name)
                if existing:
                    requests.delete(
                        f"{self.api}/releases/assets/{existing['id']}",
                        headers=self.headers,
                        timeout=60,
                    )
                    continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise MediaError(f"GitHub asset upload failed with HTTP {response.status_code}")
        raise MediaError("GitHub asset upload did not complete after retries")


@dataclass
class MediaInfo:
    url: str
    content_type: str
    directly_served: bool
    size: Optional[int]


def verify_public_media(url: str, expected_size: int, *, require_direct_video: bool = False) -> MediaInfo:
    """Check the URL Buffer will fetch, with a Range request and MP4 magic bytes."""
    last_status = None
    for delay in (3, 5, 7, 10, 10, 15):
        time.sleep(delay)
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": "DateFlow-Pipeline/1.0",
                    "Range": "bytes=0-2047",
                    "Accept": "video/mp4,application/octet-stream,*/*",
                },
                allow_redirects=True,
                timeout=60,
            )
        except requests.RequestException:
            last_status = "network error"
            continue
        last_status = response.status_code
        if response.status_code not in {200, 206}:
            continue
        body = response.content[:2048]
        if b"ftyp" not in body:
            raise MediaError("public media URL did not return MP4 bytes")
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        directly = not response.history
        if require_direct_video:
            if not directly:
                raise MediaError("configured PUBLIC_MEDIA_BASE_URL redirects; Buffer expects a direct URL")
            if not content_type.startswith("video/"):
                raise MediaError(
                    f"configured PUBLIC_MEDIA_BASE_URL serves {content_type or 'an unknown type'}, not video/mp4"
                )
        size = expected_size
        content_range = response.headers.get("Content-Range") or ""
        match = re.search(r"/(\d+)$", content_range)
        if match:
            size = int(match.group(1))
        return MediaInfo(url=url, content_type=content_type, directly_served=directly, size=size)
    raise MediaError(f"public media URL was not reachable after retries (last status: {last_status})")


def release_tag(slot: datetime) -> str:
    """One release per TRT month keeps every release below GitHub's 1,000-asset cap."""
    return f"{RELEASE_PREFIX}-{slot.astimezone(TRT).strftime('%Y-%m')}"


def public_media_url(cfg: Config, asset_name: str, release_url: str) -> tuple[str, bool]:
    """Return (url, requires_direct_video_check).

    The release URL is kept as the default because it is the durable archive.
    If PUBLIC_MEDIA_BASE_URL is configured (for example a GitHub Pages or R2
    prefix), Buffer receives that direct URL instead.
    """
    if cfg.public_media_base:
        return cfg.public_media_base.rstrip("/") + "/" + quote(asset_name), True
    return release_url, False


# ── Telegram ────────────────────────────────────────────────────────────────


def notify_telegram(cfg: Config, text: str) -> None:
    if not cfg.telegram_token or not cfg.telegram_chat:
        print("  telegram: not configured; skipping notification")
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
            json={
                "chat_id": cfg.telegram_chat,
                "text": text[:4000],
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if response.status_code != 200:
            print(f"  telegram: send failed with HTTP {response.status_code}")
    except requests.RequestException as exc:
        print(f"  telegram: send failed ({type(exc).__name__})")


# ── Orchestration ───────────────────────────────────────────────────────────


def _buffer_configured(cfg: Config) -> bool:
    return bool(cfg.buffer_key and cfg.buffer_org and cfg.buffer_channel and cfg.buffer_bin)


def _future_window(now: datetime) -> tuple[datetime, datetime]:
    return now - timedelta(days=1), now + timedelta(days=3)


def _history_window(now: datetime) -> tuple[datetime, datetime]:
    return now - timedelta(days=45), now - timedelta(days=1)


def _hooks_from_posts(posts: Iterable[ExistingPost]) -> list[str]:
    hooks: list[str] = []
    for post in posts:
        if not post.text:
            continue
        first = post.text.splitlines()[0].strip()
        first = re.sub(r"^[^A-Za-z0-9]+", "", first)
        if first:
            hooks.append(first[:120])
    return hooks


def clear_errored_slots(cfg: Config, posts: list[ExistingPost], now: datetime) -> list[ExistingPost]:
    if not cfg.replace_errored:
        return posts
    kept: list[ExistingPost] = []
    for post in posts:
        if post.status == "error" and post.due_at and post.due_at >= now - SLOT_TOLERANCE:
            try:
                buffer_delete_post(cfg, post.id)
                print(f"  removed errored Buffer post {post.id[:8]}... so the slot can be retried")
                continue
            except BufferError as exc:
                print(f"  warning: could not remove errored post: {exc}")
        kept.append(post)
    return kept


def audit_recent_errors(cfg: Config, now: datetime) -> int:
    try:
        posts = buffer_list_posts(
            cfg,
            now - timedelta(days=1),
            now,
            statuses=("error", "sent"),
            purpose="audit",
            allow_truncation=True,
        )
    except BufferError as exc:
        print(f"  audit: could not read recent posts: {exc}")
        return 0
    count = sum(1 for post in posts if post.status == "error")
    if count:
        print(f"  audit: {count} Buffer post(s) from the last 24h are in error state")
    return count


def cmd_dry_run(cfg: Config) -> int:
    now = trt_now()
    print(f"DRY RUN at {now.strftime('%Y-%m-%d %H:%M:%S %Z')} (no render, upload or publish)")
    if cfg.target_date and cfg.target_date < now.date():
        raise ConfigError("target date is in the past (TRT)")

    posts: list[ExistingPost] = []
    history: list[ExistingPost] = []
    if not cfg.offline and _buffer_configured(cfg):
        start, end = _future_window(now)
        posts = buffer_list_posts(cfg, start, end, purpose="future")
        h_start, h_end = _history_window(now)
        try:
            history = buffer_list_posts(
                cfg, h_start, h_end, statuses=("sent",), purpose="history", allow_truncation=True
            )
        except BufferError as exc:
            print(f"  warning: could not read Buffer history: {exc}")
    else:
        print("  Buffer is not configured (or --offline): slot occupancy was not checked")

    plan = plan_slots(now, posts, cfg.target_date, cfg.max_posts, cfg.max_queue)
    print(
        f"  queue={plan.queue_count} free_slots={len(plan.slots)} "
        f"blocked={len(plan.blocked)} reason={plan.reason or 'ok'}"
    )
    avoid = _hooks_from_posts(posts) + _hooks_from_posts(history)
    used: list[str] = list(avoid)
    output: list[dict[str, Any]] = []

    for slot in plan.slots:
        script = pick_script(cfg, used)
        used.append(script["hook"])
        payload = build_buffer_payload(cfg, script, slot, "https://example.invalid/reel.mp4")
        validate_payload_shape(payload)
        payload_valid = False
        if cfg.buffer_bin and not cfg.offline:
            buffer_validate_payload(cfg, payload)
            payload_valid = True
        output.append(
            {
                "slot_trt": slot.isoformat(),
                "hook": script["hook"],
                "cta": script["cta"],
                "terms": script["terms"],
                "caption_chars": len(caption_for(script)),
                "payload_valid": payload_valid,
            }
        )
        print(f"  planned {slot_label(slot)}: {script['hook']}")

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = cfg.output_dir / "dry_run_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "mode": "dry_run",
                "generated_at": now.isoformat(),
                "slots": output,
                "blocked": [{"id": post.id, "status": post.status} for post in plan.blocked],
                "reason": plan.reason,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  wrote {plan_path}")
    if plan.blocked:
        print("  blocked slots exist; resolve them in Buffer before a production run")
        return 1
    return 0


def _slot_blocker(cfg: Config, slot: datetime) -> Optional[ExistingPost]:
    posts = buffer_list_posts(
        cfg,
        slot - timedelta(minutes=30),
        slot + timedelta(minutes=30),
        purpose="slot check",
    )
    for post in posts:
        if post.matches_slot(slot):
            return post
    return None


def cmd_production(cfg: Config) -> int:
    require_production_config(cfg)
    now = trt_now()
    print(f"PRODUCTION at {now.strftime('%Y-%m-%d %H:%M:%S %Z')} event={cfg.event or 'local'}")
    if cfg.target_date and cfg.target_date < now.date():
        raise ConfigError("target date is in the past (TRT)")

    start, end = _future_window(now)
    posts = buffer_list_posts(cfg, start, end, purpose="future")
    posts = clear_errored_slots(cfg, posts, now)

    plan = plan_slots(now, posts, cfg.target_date, cfg.max_posts, cfg.max_queue)
    print(
        f"  queue={plan.queue_count} free_slots={len(plan.slots)} "
        f"blocked={len(plan.blocked)} reason={plan.reason or 'ok'}"
    )
    for post in plan.blocked:
        print(f"  blocked slot: Buffer post {post.id[:8]}... has status {post.status}")

    if not plan.slots:
        # Empty run: normal success, and deliberately no Telegram message.
        # History and audit calls are skipped to stay well inside the
        # Buffer Free API quota of 1,000 requests per channel per month.
        print("  nothing to schedule; this is not an error (no Telegram message sent)")
        if plan.blocked:
            lines = ["DateFlow: no slots scheduled; existing Buffer posts block the queue"]
            lines.extend(f"- {post.status} (id {post.id[:8]})" for post in plan.blocked[:5])
            notify_telegram(cfg, "\n".join(lines))
            return 1
        return 0

    try:
        h_start, h_end = _history_window(now)
        history = buffer_list_posts(
            cfg, h_start, h_end, statuses=("sent",), purpose="history", allow_truncation=True
        )
    except BufferError as exc:
        print(f"  warning: Buffer history is unavailable ({exc}); dedupe uses the current queue only")
        history = []
    recent_errors = audit_recent_errors(cfg, now)

    avoid = _hooks_from_posts(posts) + _hooks_from_posts(history)
    used = list(avoid)
    successes: list[tuple[datetime, dict[str, str], str]] = []
    failures: list[str] = []
    skipped: list[str] = []
    uploader = ReleaseUploader(cfg)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    consecutive_failures = 0

    for slot in plan.slots:
        label = slot_label(slot)
        script: Optional[dict[str, str]] = None
        try:
            blocker = _slot_blocker(cfg, slot)
            if blocker:
                skipped.append(f"{label}: slot already occupied ({blocker.status})")
                print(f"  skip {label}: slot is already occupied")
                continue

            script = pick_script(cfg, used)
            used.append(script["hook"])
            print(f"  slot {label}: {script['hook']}")

            rendered = render_with_mpt(cfg, script)
            asset_name = f"reel_{slot.astimezone(TRT).strftime('%Y-%m-%d_%H%M')}.mp4"
            local_copy = cfg.output_dir / asset_name
            shutil.copy2(rendered, local_copy)

            release_url = uploader.upload(local_copy, release_tag(slot))
            media_url, require_direct = public_media_url(cfg, asset_name, release_url)
            media = verify_public_media(
                media_url, local_copy.stat().st_size, require_direct_video=require_direct
            )
            if not media.directly_served:
                print(
                    "  note: GitHub Release URLs redirect and are served as "
                    f"{media.content_type or 'application/octet-stream'}; Buffer may prefer "
                    "PUBLIC_MEDIA_BASE_URL (GitHub Pages/R2)."
                )

            payload = build_buffer_payload(cfg, script, slot, media_url)
            validate_payload_shape(payload)
            buffer_validate_payload(cfg, payload)

            blocker = _slot_blocker(cfg, slot)
            if blocker:
                skipped.append(f"{label}: slot became occupied before create ({blocker.status})")
                print(f"  skip {label}: slot became occupied before create")
                continue

            post = buffer_create_post(cfg, payload, dry_run=False)
            status = str(post.get("status") or "").lower()
            post_id = str(post.get("id") or "")
            if status not in SUCCESS_POST_STATUSES:
                raise BufferError(
                    f"Buffer created post {post_id[:8]}... with status {status!r}; expected scheduled",
                    fatal=False,
                )
            if not post_id:
                raise BufferError("Buffer create returned a post without an id", fatal=False)
            verified = buffer_post_get(cfg, post_id)
            verified_status = str(verified.get("status") or "").lower()
            if verified_status not in SUCCESS_POST_STATUSES:
                raise BufferError(
                    f"Buffer post {post_id[:8]}... ended in status {verified_status or 'unknown'!r}",
                    fatal=False,
                )
            successes.append((slot, script, post_id))
            consecutive_failures = 0
            print(f"  scheduled {label} as Buffer post {post_id[:8]}... (status {verified_status})")
        except BufferError as exc:
            message = redact_for_config(str(exc), cfg)
            # `posts create` is not idempotent. After an ambiguous failure, look
            # at the slot once: if Buffer did create the post, this is a success,
            # not a duplicate to retry.
            recovered = None
            if script is not None:
                try:
                    recovered = _slot_blocker(cfg, slot)
                except Exception:  # noqa: BLE001 - recovery check must not mask the original error
                    recovered = None
            if recovered is not None and recovered.status in SUCCESS_POST_STATUSES:
                successes.append((slot, script, recovered.id))
                consecutive_failures = 0
                print(f"  recovered {label}: post {recovered.id[:8]}... exists with status {recovered.status}")
                continue
            failures.append(f"{label}: {message}")
            print(f"  FAILED {label}: {message}")
            consecutive_failures += 1
            if exc.fatal or consecutive_failures >= 2:
                failures.append("aborting remaining slots after repeated or fatal Buffer errors")
                break
        except Exception as exc:  # noqa: BLE001 - one slot must not hide the others
            message = redact_for_config(str(exc), cfg)
            failures.append(f"{label}: {message}")
            print(f"  FAILED {label}: {message}")
            traceback.print_exc(limit=3)
            consecutive_failures += 1

    lines = []
    if successes:
        lines.append(f"DateFlow: {len(successes)}/{len(plan.slots)} Reels scheduled")
        for slot, script, post_id in successes:
            lines.append(f"- {slot_label(slot)} — {script['hook']} (post {post_id[:8]})")
    if skipped:
        lines.append("Skipped:")
        lines.extend(f"- {item}" for item in skipped)
    if failures:
        lines.append("Failed:")
        lines.extend(f"- {item}" for item in failures[:6])
    if plan.blocked:
        lines.append("Blocked by existing posts:")
        for post in plan.blocked[:4]:
            lines.append(f"- {post.status} (id {post.id[:8]})")
    if recent_errors:
        lines.append(f"Audit: {recent_errors} post(s) from the last 24h are in error state")
    run_url = (
        f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/"
        f"{cfg.github_repository}/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}"
        if cfg.github_repository and os.getenv("GITHUB_RUN_ID")
        else ""
    )
    if run_url:
        lines.append(run_url)
    summary = "\n".join(lines).strip()
    if summary:
        print(summary)
        if successes or failures or plan.blocked:
            notify_telegram(cfg, summary)

    if failures or plan.blocked:
        return 1
    return 0


# ── Entry point ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DateFlow Reels autopilot")
    parser.add_argument("--mode", choices=("dry_run", "production"), default=None)
    parser.add_argument("--date", default="", help="TRT date YYYY-MM-DD (manual catch-up)")
    parser.add_argument("--max-posts", type=int, default=None, dest="max_posts")
    parser.add_argument("--confirm", default="", help="must be PUBLISH for a manual production run")
    parser.add_argument("--offline", action="store_true", help="no network calls (dry-run tests)")
    parser.add_argument("--no-llm", action="store_true", help="skip the free LLM and use the fallback pool")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg: Optional[Config] = None
    try:
        cfg = load_config(args)
        if cfg.mode == "production" and cfg.event == "workflow_dispatch" and cfg.confirm != "PUBLISH":
            raise ConfigError(
                "manual production requires --confirm PUBLISH (or the workflow's confirm input)"
            )
        if cfg.mode == "production":
            return cmd_production(cfg)
        return cmd_dry_run(cfg)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        if cfg and cfg.mode == "production":
            notify_telegram(cfg, f"DateFlow configuration error: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        safe = redact_for_config(str(exc), cfg) if cfg else str(exc)
        print(f"error: {safe}", file=sys.stderr)
        if cfg and cfg.mode == "production":
            notify_telegram(cfg, f"DateFlow pipeline failed: {safe[:500]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
