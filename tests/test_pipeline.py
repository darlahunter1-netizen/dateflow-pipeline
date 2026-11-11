"""Offline tests for the DateFlow pipeline.

These tests never call Buffer, Gemini, Pexels, Telegram or GitHub.
Run with: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline  # noqa: E402


def make_config(**overrides):
    defaults = dict(
        mode="production",
        buffer_bin="buffer",
        buffer_key="test-buffer-key",
        buffer_org="test-org",
        buffer_channel="test-channel",
        github_token="test-github-token",
        github_repository="owner/repo",
        pexels_key="test-pexels-key",
        output_dir=Path(tempfile.mkdtemp(prefix="dateflow-test-")),
    )
    defaults.update(overrides)
    return pipeline.Config(**defaults)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["buffer"], returncode=returncode, stdout=stdout, stderr=stderr)


class SlotPlanningTests(unittest.TestCase):
    def test_exact_required_slots(self):
        self.assertEqual(pipeline.POST_TIMES, ("11:00", "15:00", "18:15", "21:30"))

    def test_after_last_slot_plans_all_four_for_tomorrow(self):
        now = datetime(2026, 11, 11, 23, 10, tzinfo=pipeline.TRT)
        plan = pipeline.plan_slots(now, [], max_posts=4)
        self.assertEqual(
            [slot.strftime("%Y-%m-%d %H:%M") for slot in plan.slots],
            [
                "2026-11-12 11:00",
                "2026-11-12 15:00",
                "2026-11-12 18:15",
                "2026-11-12 21:30",
            ],
        )
        self.assertEqual(plan.reason, "")

    def test_midday_only_returns_future_slots(self):
        now = datetime(2026, 11, 11, 12, 10, tzinfo=pipeline.TRT)
        plan = pipeline.plan_slots(now, [])
        self.assertEqual([slot.strftime("%H:%M") for slot in plan.slots], ["15:00", "18:15", "21:30"])

    def test_occupied_slot_is_skipped(self):
        now = datetime(2026, 11, 11, 10, 0, tzinfo=pipeline.TRT)
        slot = pipeline.slot_datetime(now.date(), "11:00")
        existing = [pipeline.ExistingPost("p1", "scheduled", slot.astimezone(pipeline.UTC))]
        plan = pipeline.plan_slots(now, existing)
        self.assertEqual([s.strftime("%H:%M") for s in plan.slots], ["15:00", "18:15", "21:30"])
        self.assertFalse(plan.blocked)

    def test_never_returns_past_slot(self):
        now = datetime(2026, 11, 11, 19, 0, tzinfo=pipeline.TRT)
        plan = pipeline.plan_slots(now, [])
        self.assertEqual([s.strftime("%H:%M") for s in plan.slots], ["21:30"])
        self.assertTrue(all(s > now for s in plan.slots))

    def test_last_slot_inside_lead_window_rolls_to_tomorrow(self):
        now = datetime(2026, 11, 11, 21, 20, tzinfo=pipeline.TRT)
        plan = pipeline.plan_slots(now, [], max_posts=4)
        self.assertEqual(
            [slot.strftime("%Y-%m-%d %H:%M") for slot in plan.slots],
            [
                "2026-11-12 11:00",
                "2026-11-12 15:00",
                "2026-11-12 18:15",
                "2026-11-12 21:30",
            ],
        )

    def test_target_date_only_does_not_roll_forward(self):
        now = datetime(2026, 11, 11, 23, 10, tzinfo=pipeline.TRT)
        plan = pipeline.plan_slots(now, [], target_day=now.date(), max_posts=4)
        self.assertEqual(plan.slots, [])
        self.assertEqual(plan.reason, "no_future_slots")

    def test_queue_capacity_is_respected(self):
        now = datetime(2026, 11, 11, 10, 0, tzinfo=pipeline.TRT)
        existing = [
            pipeline.ExistingPost(
                f"p{i}",
                "scheduled",
                (now + timedelta(days=1, minutes=i)).astimezone(pipeline.UTC),
            )
            for i in range(9)
        ]
        plan = pipeline.plan_slots(now, existing, max_queue=10)
        self.assertEqual(len(plan.slots), 1)
        self.assertEqual(plan.queue_count, 9)

        full = existing + [
            pipeline.ExistingPost("p10", "scheduled", (now + timedelta(days=1)).astimezone(pipeline.UTC))
        ]
        plan = pipeline.plan_slots(now, full, max_queue=10)
        self.assertEqual(plan.slots, [])
        self.assertEqual(plan.reason, "queue_capacity")

    def test_needs_approval_blocks_slot_but_is_not_a_crash(self):
        now = datetime(2026, 11, 11, 10, 0, tzinfo=pipeline.TRT)
        slot = pipeline.slot_datetime(now.date(), "15:00")
        existing = [pipeline.ExistingPost("p2", "needs_approval", slot.astimezone(pipeline.UTC))]
        plan = pipeline.plan_slots(now, existing)
        self.assertEqual([s.strftime("%H:%M") for s in plan.slots], ["11:00", "18:15", "21:30"])
        self.assertEqual(len(plan.blocked), 1)
        self.assertEqual(plan.blocked[0].status, "needs_approval")

    def test_past_due_posts_do_not_block_future_slots(self):
        now = datetime(2026, 11, 11, 16, 0, tzinfo=pipeline.TRT)
        past = pipeline.slot_datetime(now.date(), "11:00").astimezone(pipeline.UTC)
        existing = [pipeline.ExistingPost("old", "sent", past)]
        plan = pipeline.plan_slots(now, existing)
        self.assertEqual([s.strftime("%H:%M") for s in plan.slots], ["18:15", "21:30"])


class ScriptValidationTests(unittest.TestCase):
    def valid_script(self):
        return dict(pipeline.FALLBACK_SCRIPTS[0])

    def test_all_fallback_scripts_are_valid_and_have_all_fields(self):
        for script in pipeline.FALLBACK_SCRIPTS:
            for key in ("hook", "body", "cta", "terms"):
                self.assertIn(key, script)
                self.assertTrue(script[key].strip())
            ok, reason = pipeline.validate_script(script)
            self.assertTrue(ok, f"{script['hook']}: {reason}")

    def test_fallback_hooks_are_unique(self):
        hooks = [pipeline.normalize_hook(script["hook"]) for script in pipeline.FALLBACK_SCRIPTS]
        self.assertEqual(len(hooks), len(set(hooks)))

    def test_missing_cta_is_rejected_not_crashing(self):
        script = self.valid_script()
        del script["cta"]
        ok, reason = pipeline.validate_script(script)
        self.assertFalse(ok)
        self.assertIn("cta", reason)

    def test_missing_terms_is_rejected(self):
        script = self.valid_script()
        del script["terms"]
        ok, reason = pipeline.validate_script(script)
        self.assertFalse(ok)
        self.assertIn("terms", reason)

    def test_terms_list_is_normalized(self):
        script = self.valid_script()
        script["terms"] = ["coffee date couple", "city walk couple", "sunset couple"]
        normalized = pipeline.normalize_script(script)
        self.assertEqual(normalized["terms"], "coffee date couple, city walk couple, sunset couple")

    def test_json_fences_are_stripped(self):
        raw = "```json\n" + json.dumps(self.valid_script()) + "\n```"
        self.assertEqual(pipeline.parse_script_json(raw)["hook"], self.valid_script()["hook"])

    def test_prose_without_json_is_rejected(self):
        with self.assertRaises(ValueError):
            pipeline.parse_script_json("Here is your script: do something nice.")

    def test_coercive_language_is_rejected(self):
        script = self.valid_script()
        script["body"] = "Ignore her no and keep pushing until she agrees. That is how you win."
        ok, reason = pipeline.validate_script(script)
        self.assertFalse(ok)
        self.assertIn("coercive", reason)

    def test_emoji_hashtag_and_url_are_rejected(self):
        script = self.valid_script()
        script["body"] = "Plan one hour, not five #dateideas and book at https://example.com"
        ok, reason = pipeline.validate_script(script)
        self.assertFalse(ok)
        self.assertIn("markdown", reason)

    def test_similar_hook_is_rejected(self):
        existing = [self.valid_script()["hook"]]
        script = self.valid_script()
        script["hook"] = script["hook"].upper() + "!"
        ok, reason = pipeline.validate_script(script, existing)
        self.assertFalse(ok)
        self.assertIn("similar", reason)

    def test_caption_has_six_hashtags_and_fits_limit(self):
        caption = pipeline.caption_for(self.valid_script())
        self.assertLessEqual(len(caption), 2200)
        self.assertEqual(len(re.findall(r"#\w+", caption)), 6)
        self.assertIn(self.valid_script()["cta"], caption)

    def test_full_text_adds_terminal_punctuation(self):
        text = pipeline.full_script_text(self.valid_script())
        self.assertTrue(text.startswith(self.valid_script()["hook"] + "."))
        self.assertIn(self.valid_script()["cta"], text)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        mpt = Path(tempfile.mkdtemp(prefix="dateflow-mpt-"))
        (mpt / "cli.py").write_text("# stub\n", encoding="utf-8")
        self.cfg = make_config(mpt_dir=mpt)
        self.script = dict(pipeline.FALLBACK_SCRIPTS[0])
        self.slot = datetime(2026, 11, 12, 11, 0, tzinfo=pipeline.TRT)

    def test_payload_uses_reel_contract_and_trt_offset(self):
        payload = pipeline.build_buffer_payload(
            self.cfg, self.script, self.slot, "https://example.com/reel.mp4"
        )
        self.assertEqual(payload["mode"], "customScheduled")
        self.assertEqual(payload["schedulingType"], "automatic")
        self.assertTrue(payload["dueAt"].endswith("+03:00"))
        self.assertEqual(payload["metadata"]["instagram"]["type"], "reel")
        self.assertTrue(payload["metadata"]["instagram"]["shouldShareToFeed"])
        self.assertEqual(payload["assets"][0]["video"]["url"], "https://example.com/reel.mp4")
        self.assertEqual(payload["assets"][0]["video"]["metadata"]["thumbnailOffset"], 2000)
        self.assertNotIn("thumbnailUrl", payload["assets"][0]["video"])
        pipeline.validate_payload_shape(payload)

    def test_payload_without_cta_raises_a_clear_error(self):
        broken = dict(self.script)
        broken.pop("cta")
        with self.assertRaises(pipeline.DateFlowError):
            pipeline.build_buffer_payload(self.cfg, broken, self.slot, "https://example.com/reel.mp4")

    def test_payload_rejects_http_url(self):
        with self.assertRaises(pipeline.DateFlowError):
            pipeline.build_buffer_payload(self.cfg, self.script, self.slot, "http://example.com/reel.mp4")

    def test_payload_shape_rejects_missing_reel_type(self):
        payload = pipeline.build_buffer_payload(
            self.cfg, self.script, self.slot, "https://example.com/reel.mp4"
        )
        payload["metadata"]["instagram"]["type"] = "post"
        with self.assertRaises(pipeline.DateFlowError):
            pipeline.validate_payload_shape(payload)

    def test_list_fields_explicitly_include_due_at(self):
        # Buffer's default field set is items.id/items.text/items.status and
        # omits dueAt. Requesting dueAt explicitly is required for slot dedupe.
        self.assertIn("dueAt", pipeline.BUFFER_LIST_FIELDS)
        self.assertIn("pageInfo", pipeline.BUFFER_LIST_FIELDS)


class VideoGateTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="dateflow-video-")) / "reel.mp4"
        self.path.write_bytes(b"\x00" * 600_000)

    def info(self, **overrides):
        base = {
            "width": 1080,
            "height": 1920,
            "codec": "h264",
            "audio_codec": "aac",
            "duration": 24.0,
        }
        base.update(overrides)
        return base

    def test_accepts_a_valid_reel(self):
        with patch.object(pipeline, "_ffprobe_info", return_value=self.info()):
            pipeline.verify_video(self.path)

    def test_rejects_landscape(self):
        with patch.object(pipeline, "_ffprobe_info", return_value=self.info(width=1920, height=1080)):
            with self.assertRaises(pipeline.RenderError):
                pipeline.verify_video(self.path)

    def test_rejects_missing_audio(self):
        with patch.object(pipeline, "_ffprobe_info", return_value=self.info(audio_codec="")):
            with self.assertRaises(pipeline.RenderError):
                pipeline.verify_video(self.path)

    def test_rejects_non_h264(self):
        with patch.object(pipeline, "_ffprobe_info", return_value=self.info(codec="vp9")):
            with self.assertRaises(pipeline.RenderError):
                pipeline.verify_video(self.path)


class ReleaseTagTests(unittest.TestCase):
    def test_monthly_release_tag_uses_trt_month(self):
        slot = datetime(2026, 11, 12, 1, 0, tzinfo=pipeline.UTC)  # 04:00 TRT, same month
        self.assertEqual(pipeline.release_tag(slot), "dateflow-media-2026-11")
        slot = datetime(2026, 10, 31, 22, 0, tzinfo=pipeline.UTC)  # 01:00 TRT Nov 1
        self.assertEqual(pipeline.release_tag(slot), "dateflow-media-2026-11")


class BufferParsingTests(unittest.TestCase):
    def setUp(self):
        mpt = Path(tempfile.mkdtemp(prefix="dateflow-mpt-"))
        (mpt / "cli.py").write_text("# stub\n", encoding="utf-8")
        self.cfg = make_config(mpt_dir=mpt)
        self.start = datetime(2026, 11, 10, tzinfo=pipeline.UTC)
        self.end = datetime(2026, 11, 15, tzinfo=pipeline.UTC)

    def test_real_list_shape_is_parsed(self):
        payload = {
            "items": [
                {
                    "id": "post-1",
                    "status": "scheduled",
                    "dueAt": "2026-11-12T08:00:00.000Z",
                    "text": "A real hook\n\nbody",
                },
                {
                    "id": "post-2",
                    "status": "needs_approval",
                    "dueAt": "2026-11-12T12:00:00Z",
                    "text": "Another hook",
                },
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
        with patch.object(pipeline, "_run_buffer", return_value=completed(json.dumps(payload))):
            posts = pipeline.buffer_list_posts(self.cfg, self.start, self.end)
        self.assertEqual([post.id for post in posts], ["post-1", "post-2"])
        self.assertEqual(posts[0].status, "scheduled")
        self.assertEqual(posts[0].due_at, datetime(2026, 11, 12, 8, 0, tzinfo=pipeline.UTC))
        self.assertEqual(posts[1].status, "needs_approval")

    def test_queue_check_does_not_filter_away_legacy_statuses(self):
        cfg = make_config()
        captured = {}

        def fake_run(cfg, args, timeout=90):
            captured["args"] = args
            return completed(stdout=json.dumps({"items": [], "pageInfo": {"hasNextPage": False}}))

        with patch.object(pipeline, "_run_buffer", side_effect=fake_run):
            pipeline.buffer_list_posts(cfg, datetime(2026, 11, 11, tzinfo=pipeline.UTC), datetime(2026, 11, 14, tzinfo=pipeline.UTC))

        payload = json.loads(captured["args"][captured["args"].index("--json") + 1])
        self.assertNotIn("status", payload["filter"])
        self.assertIn("dueAt", payload["filter"])

    def test_truncated_page_is_refused_for_write_safety(self):
        payload = {"items": [], "pageInfo": {"hasNextPage": True, "endCursor": "x"}}
        with patch.object(pipeline, "_run_buffer", return_value=completed(json.dumps(payload))):
            with self.assertRaises(pipeline.BufferError):
                pipeline.buffer_list_posts(self.cfg, self.start, self.end)

    def test_truncated_history_can_be_accepted(self):
        payload = {"items": [], "pageInfo": {"hasNextPage": True, "endCursor": "x"}}
        with patch.object(pipeline, "_run_buffer", return_value=completed(json.dumps(payload))):
            posts = pipeline.buffer_list_posts(
                self.cfg, self.start, self.end, statuses=("sent",), purpose="history", allow_truncation=True
            )
        self.assertEqual(posts, [])

    def test_auth_error_is_marked_fatal(self):
        with patch.object(
            pipeline,
            "_run_buffer",
            return_value=completed(
                stdout=json.dumps({"error": "AUTH_ERROR", "code": 4, "message": "Access token is not valid"}),
                returncode=4,
            ),
        ):
            with self.assertRaises(pipeline.BufferError) as ctx:
                pipeline.buffer_list_posts(self.cfg, self.start, self.end)
        self.assertTrue(ctx.exception.fatal)

    def test_create_success_shape(self):
        payload = {
            "post": {"id": "post-123", "status": "scheduled"},
        }
        with patch.object(pipeline, "_run_buffer", return_value=completed(json.dumps(payload))):
            post = pipeline.buffer_create_post(
                self.cfg,
                {
                    "channelId": "test-channel",
                    "schedulingType": "automatic",
                    "mode": "customScheduled",
                    "dueAt": "2026-11-12T08:00:00+03:00",
                    "text": "text",
                },
                dry_run=False,
            )
        self.assertEqual(post["id"], "post-123")
        self.assertEqual(post["status"], "scheduled")

    def test_create_without_post_is_an_error(self):
        with patch.object(
            pipeline, "_run_buffer", return_value=completed(json.dumps({"message": "MutationError"}))
        ):
            with self.assertRaises(pipeline.BufferError):
                pipeline.buffer_create_post(self.cfg, {"channelId": "c"}, dry_run=False)

    def test_extract_post_from_union_payload(self):
        data = {"createPost": {"__typename": "PostActionSuccess", "post": {"id": "abc", "status": "scheduled"}}}
        self.assertEqual(pipeline._extract_post(data)["id"], "abc")

    def test_status_sets_match_buffer_enum(self):
        self.assertTrue({"draft", "error", "needs_approval", "scheduled", "sending", "sent"}.issubset(
            pipeline.OCCUPIED_STATUSES
        ))
        self.assertIn("buffer", pipeline.OCCUPIED_STATUSES)
        self.assertNotIn("pending", pipeline.OCCUPIED_STATUSES)
        self.assertNotIn("failed", pipeline.OCCUPIED_STATUSES)

    def test_errored_slots_can_be_cleared(self):
        now = datetime(2026, 11, 11, 10, 0, tzinfo=pipeline.TRT)
        slot = pipeline.slot_datetime(now.date(), "18:15")
        error_post = pipeline.ExistingPost("err-1", "error", slot.astimezone(pipeline.UTC))
        other = pipeline.ExistingPost("ok-1", "scheduled", slot.astimezone(pipeline.UTC))
        with patch.object(pipeline, "buffer_delete_post") as delete:
            kept = pipeline.clear_errored_slots(self.cfg, [error_post, other], now)
        delete.assert_called_once_with(self.cfg, "err-1")
        self.assertEqual([post.id for post in kept], ["ok-1"])


class ProductionWiringTests(unittest.TestCase):
    """Exercise cmd_production end-to-end with every network call mocked."""

    def setUp(self):
        mpt = Path(tempfile.mkdtemp(prefix="dateflow-mpt-"))
        (mpt / "cli.py").write_text("# stub\n", encoding="utf-8")
        self.cfg = make_config(mpt_dir=mpt)
        self.script = {
            "hook": "A busy schedule is not a status symbol",
            "body": "Plan one short date instead of a five hour marathon and both calendars stay open. A coffee walk leaves room for a second date, and you learn more in an hour than in a month of texting.",
            "cta": "Let DateFlow plan the next one",
            "terms": "couple coffee shop, city walk couple, cafe window light",
        }
        self.sent = []

    def test_full_batch_schedules_every_planned_slot(self):
        created = []

        def fake_create(cfg, payload, *, dry_run):
            created.append(payload)
            return {"id": f"post-{len(created)}", "status": "scheduled"}

        late = datetime(2026, 11, 11, 23, 10, tzinfo=pipeline.TRT)
        with patch.object(pipeline, "trt_now", return_value=late), patch.object(
            pipeline, "buffer_list_posts", return_value=[]
        ), patch.object(
            pipeline, "_slot_blocker", return_value=None
        ), patch.object(pipeline, "pick_script", return_value=self.script), patch.object(
            pipeline, "render_with_mpt", side_effect=self._render
        ), patch.object(
            pipeline.ReleaseUploader, "upload", return_value="https://example.com/reel.mp4"
        ), patch.object(
            pipeline,
            "verify_public_media",
            return_value=pipeline.MediaInfo("https://example.com/reel.mp4", "video/mp4", False, 100),
        ), patch.object(
            pipeline, "buffer_validate_payload", return_value=None
        ), patch.object(
            pipeline, "buffer_create_post", side_effect=fake_create
        ), patch.object(
            pipeline, "buffer_post_get", return_value={"id": "post-1", "status": "scheduled"}
        ), patch.object(
            pipeline, "notify_telegram", side_effect=lambda cfg, text: self.sent.append(text)
        ), patch.object(
            pipeline, "audit_recent_errors", return_value=0
        ):
            exit_code = pipeline.cmd_production(self.cfg)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(created), 4)
        self.assertEqual(len(self.sent), 1)
        for payload in created:
            self.assertEqual(payload["metadata"]["instagram"]["type"], "reel")
            self.assertTrue(payload["assets"][0]["video"]["url"].startswith("https://"))
            self.assertEqual(payload["mode"], "customScheduled")

    def test_failed_create_exits_non_zero_and_notifies(self):
        def boom(cfg, payload, *, dry_run):
            raise pipeline.BufferError("Buffer create error: InvalidInputError", fatal=False)

        late = datetime(2026, 11, 11, 23, 10, tzinfo=pipeline.TRT)
        with patch.object(pipeline, "trt_now", return_value=late), patch.object(
            pipeline, "buffer_list_posts", return_value=[]
        ), patch.object(
            pipeline, "_slot_blocker", return_value=None
        ), patch.object(pipeline, "pick_script", return_value=self.script), patch.object(
            pipeline, "render_with_mpt", side_effect=self._render
        ), patch.object(
            pipeline.ReleaseUploader, "upload", return_value="https://example.com/reel.mp4"
        ), patch.object(
            pipeline,
            "verify_public_media",
            return_value=pipeline.MediaInfo("https://example.com/reel.mp4", "video/mp4", False, 100),
        ), patch.object(
            pipeline, "buffer_validate_payload", return_value=None
        ), patch.object(
            pipeline, "buffer_create_post", side_effect=boom
        ), patch.object(
            pipeline, "notify_telegram", side_effect=lambda cfg, text: self.sent.append(text)
        ), patch.object(
            pipeline, "audit_recent_errors", return_value=0
        ):
            exit_code = pipeline.cmd_production(self.cfg)

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Failed", self.sent[0])

    def test_ambiguous_create_failure_is_recovered_when_the_post_exists(self):
        late = datetime(2026, 11, 11, 23, 10, tzinfo=pipeline.TRT)
        slot = pipeline.slot_datetime(datetime(2026, 11, 12).date(), "11:00")
        existing = pipeline.ExistingPost("post-9", "scheduled", slot.astimezone(pipeline.UTC))
        cfg = make_config(mpt_dir=self.cfg.mpt_dir, max_posts=1)

        def boom(cfg, payload, *, dry_run):
            raise pipeline.BufferError("Buffer CLI timed out", fatal=False)

        with patch.object(pipeline, "trt_now", return_value=late), patch.object(
            pipeline, "buffer_list_posts", return_value=[]
        ), patch.object(
            pipeline, "_slot_blocker", side_effect=[None, None, existing]
        ), patch.object(
            pipeline, "pick_script", return_value=self.script
        ), patch.object(
            pipeline, "render_with_mpt", side_effect=self._render
        ), patch.object(
            pipeline.ReleaseUploader, "upload", return_value="https://example.com/reel.mp4"
        ), patch.object(
            pipeline,
            "verify_public_media",
            return_value=pipeline.MediaInfo("https://example.com/reel.mp4", "video/mp4", False, 100),
        ), patch.object(
            pipeline, "buffer_validate_payload", return_value=None
        ), patch.object(
            pipeline, "buffer_create_post", side_effect=boom
        ), patch.object(
            pipeline, "notify_telegram", side_effect=lambda cfg, text: self.sent.append(text)
        ), patch.object(
            pipeline, "audit_recent_errors", return_value=0
        ):
            exit_code = pipeline.cmd_production(cfg)

        self.assertEqual(exit_code, 0)
        self.assertIn("1/1", self.sent[0])

    def test_empty_run_is_success_and_silent(self):
        with patch.object(pipeline, "buffer_list_posts", return_value=[]), patch.object(
            pipeline, "audit_recent_errors", return_value=0
        ), patch.object(
            pipeline, "notify_telegram", side_effect=lambda cfg, text: self.sent.append(text)
        ), patch.object(
            pipeline, "plan_slots", return_value=pipeline.Plan(slots=[], blocked=[], reason="no_future_slots")
        ):
            exit_code = pipeline.cmd_production(self.cfg)

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.sent, [])

    def _render(self, cfg, script):
        path = Path(tempfile.mkdtemp(prefix="dateflow-render-")) / "reel.mp4"
        path.write_bytes(b"\x00" * 2048)
        return path


class RepositoryHygieneTests(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_no_temporary_media_hosts_in_pipeline(self):
        source = self.read("pipeline.py").lower()
        for host in ("uguu", "imgur", "litterbox", "catbox"):
            self.assertNotIn(host, source)

    def test_no_plaintext_looking_secrets_in_tracked_config(self):
        # Patterns are assembled at runtime so this test file does not trip itself.
        patterns = [
            re.compile(r"\bsk" + r"-[A-Za-z0-9_-]{20,}"),
            re.compile(r"\b\d{8,10}" + r":[A-Za-z0-9_-]{30,}"),
            re.compile(r"\bAIza" + r"[0-9A-Za-z_-]{30,}"),
        ]
        for relative in ("pipeline.py", "README.md", "SETUP.md", "setup_buffer.sh", ".github/workflows/daily-reel.yml"):
            text = self.read(relative)
            for pattern in patterns:
                self.assertIsNone(pattern.search(text), f"possible secret in {relative}")

    def test_setup_buffer_does_not_write_keys_to_disk(self):
        script = self.read("setup_buffer.sh")
        self.assertNotIn("config.json", script)
        self.assertNotIn("cat >", script)

    def test_workflow_contract(self):
        import yaml

        data = yaml.safe_load(self.read(".github/workflows/daily-reel.yml"))
        on = data.get("on", data.get(True))
        self.assertIn("workflow_dispatch", on)
        inputs = on["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["mode"]["default"], "dry_run")
        self.assertIn("production", inputs["mode"]["options"])
        crons = [entry["cron"] for entry in on["schedule"]]
        self.assertEqual(len(crons), 3)
        self.assertIn("10 20 * * *", crons)
        self.assertEqual(data["permissions"]["contents"], "write")
        text = self.read(".github/workflows/daily-reel.yml")
        self.assertIn("@bufferapp/cli@2.3.0", text)
        self.assertIn("MoneyPrinterTurbo.git", text)
        self.assertIn("v1.3.10", text)
        self.assertNotIn("OPENAI_API_KEY", text)
        self.assertNotIn("uguu", text.lower())

    def test_keepalive_workflow_contract(self):
        import yaml

        data = yaml.safe_load(self.read(".github/workflows/keepalive.yml"))
        on = data.get("on", data.get(True))
        crons = [entry["cron"] for entry in on["schedule"]]
        self.assertEqual(crons, ["0 4 1 * *"])
        self.assertEqual(data["permissions"]["contents"], "write")
        text = self.read(".github/workflows/keepalive.yml")
        self.assertIn(".github/keepalive.txt", text)
        self.assertNotIn("BUFFER_API_KEY", text)
        self.assertNotIn("pipeline.py", text)

    def test_docs_describe_the_real_slots_and_schedule(self):
        readme = self.read("README.md")
        setup = self.read("SETUP.md")
        for document in (readme, setup):
            for slot in ("11:00", "15:00", "18:15", "21:30"):
                self.assertIn(slot, document)
            self.assertIn("60 days", document)
            self.assertNotIn("uguu", document.lower())
        self.assertIn("10 20 * * *", readme)
        self.assertIn("10 2 * * *", readme)
        self.assertIn("10 8 * * *", readme)

    def test_setup_rotation_anchor_matches_readme_link(self):
        readme = self.read("README.md")
        setup = self.read("SETUP.md")
        self.assertIn("SETUP.md#10-rotate-exposed-credentials", readme)
        heading = next(
            line for line in setup.splitlines() if line.startswith("## ") and "Rotate exposed" in line
        )
        slug = heading.lstrip("# ").strip().lower()
        slug = re.sub(r"[^a-z0-9 -]", "", slug).replace(" ", "-")
        self.assertEqual(slug, "10-rotate-exposed-credentials")

    def test_readme_does_not_claim_paid_or_proprietary_llm(self):
        readme = self.read("README.md").lower()
        self.assertNotIn("openai_api_key", readme)
        self.assertIn("no paid fallback", readme)
        self.assertIn("not guaranteed", readme)

    def test_tests_workflow_only_reads(self):
        import yaml

        data = yaml.safe_load(self.read(".github/workflows/tests.yml"))
        on = data.get("on", data.get(True))
        self.assertIn("push", on)
        text = self.read(".github/workflows/tests.yml")
        self.assertNotIn("BUFFER_API_KEY", text)
        self.assertNotIn("workflow_dispatch", on.get("schedule", []) if isinstance(on.get("schedule"), list) else "")


if __name__ == "__main__":
    unittest.main()
