"""Self-host auto-post (auto_post.py + app._auto_post_job).

What must hold: bad options are refused before a job renders, clips from
jobs finishing together are spaced on one calendar instead of posted at
once, and a failed upload is reported without stopping the others.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import app
import auto_post

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
VALID = {"platforms": ["youtube", "instagram"], "user_id": "me", "clips": 2, "interval_hours": 3}


class TestParseOptions:
    def test_absent_means_off(self):
        assert auto_post.parse_options(None) is None
        assert auto_post.parse_options("") is None

    def test_accepts_json_string_from_a_form_field(self):
        opts = auto_post.parse_options('{"platforms": ["tiktok"], "user_id": " me "}')
        assert opts == [{"platforms": ["tiktok"], "user_id": "me", "clips": 3, "interval_hours": 3.0}]

    def test_a_list_is_one_entry_per_channel(self):
        opts = auto_post.parse_options([{**VALID, "user_id": "yt", "interval_hours": 3},
                                        {**VALID, "user_id": "tt", "interval_hours": 24}])
        assert [(o["user_id"], o["interval_hours"]) for o in opts] == [("yt", 3.0), ("tt", 24.0)]

    @pytest.mark.parametrize("raw", [
        [VALID, VALID],  # same channel twice
        [{**VALID, "user_id": f"c{i}"} for i in range(auto_post.MAX_CHANNELS + 1)],
        ["not an object"],
    ])
    def test_rejects_bad_channel_lists(self, raw):
        with pytest.raises(ValueError):
            auto_post.parse_options(raw)

    @pytest.mark.parametrize("raw", [
        "{not json",
        {"platforms": [], "user_id": "me"},
        {"platforms": ["myspace"], "user_id": "me"},
        {"platforms": ["tiktok"], "user_id": "  "},
        {**VALID, "clips": 0},
        {**VALID, "clips": 16},
        {**VALID, "interval_hours": 0.1},
        {**VALID, "interval_hours": "soon"},
    ])
    def test_rejects_bad_options(self, raw):
        with pytest.raises(ValueError):
            auto_post.parse_options(raw)


def test_pick_clips_takes_best_rendered_by_score():
    clips = [
        {"video_url": "/a", "predicted_score": "70"},
        {"video_url": "/b", "predicted_score": "95"},
        {"predicted_score": "99"},  # never rendered
        {"video_url": "/d", "predicted_score": None},
    ]
    assert auto_post.pick_clips(clips, 2) == [1, 0]


class TestClaimSlots:
    def test_first_job_starts_shortly_after_now(self, tmp_path):
        slots = auto_post.claim_slots(tmp_path / "cal.json", "a", 3, 2, now=NOW)
        start = NOW + auto_post.FIRST_POST_DELAY
        assert slots == [start, start + timedelta(hours=2), start + timedelta(hours=4)]

    def test_next_job_on_a_channel_continues_after_its_last_slot(self, tmp_path):
        cal = tmp_path / "cal.json"
        first = auto_post.claim_slots(cal, "a", 2, 3, now=NOW)
        second = auto_post.claim_slots(cal, "a", 1, 3, now=NOW)
        assert second == [first[-1] + timedelta(hours=3)]

    def test_channels_do_not_wait_for_each_other(self, tmp_path):
        cal = tmp_path / "cal.json"
        auto_post.claim_slots(cal, "crypto", 5, 1, now=NOW)
        stocks = auto_post.claim_slots(cal, "stocks", 5, 1, now=NOW)
        assert stocks[0] == NOW + auto_post.FIRST_POST_DELAY

    def test_a_stale_calendar_does_not_schedule_in_the_past(self, tmp_path):
        cal = tmp_path / "cal.json"
        auto_post.claim_slots(cal, "a", 1, 3, now=NOW - timedelta(days=5))
        assert auto_post.claim_slots(cal, "a", 1, 3, now=NOW) == [NOW + auto_post.FIRST_POST_DELAY]

    @pytest.mark.parametrize("content", ["garbage", "[1, 2]", '{"a": "not a date"}',
                                         '{"last_slot": "2026-09-29T20:41:00+00:00"}'])
    def test_unreadable_or_old_format_calendar_starts_over(self, tmp_path, content):
        cal = tmp_path / "cal.json"
        cal.write_text(content)
        assert auto_post.claim_slots(cal, "a", 1, 3, now=NOW) == [NOW + auto_post.FIRST_POST_DELAY]


class TestResolveOnSubmit:
    def _resolve(self, raw):
        return asyncio.run(app._resolve_auto_post(None, raw))

    def test_cloud_mode_refuses(self, monkeypatch):
        monkeypatch.setattr(app, "BILLING_ENABLED", True)
        with pytest.raises(HTTPException) as e:
            self._resolve(VALID)
        assert e.value.status_code == 400

    def test_refuses_without_an_upload_post_key(self, monkeypatch):
        async def no_key(request, body_key):
            return None, None
        monkeypatch.setattr(app, "resolve_upload_post", no_key)
        with pytest.raises(HTTPException) as e:
            self._resolve(VALID)
        assert "Upload-Post key" in e.value.detail

    def test_bad_options_are_a_400(self):
        with pytest.raises(HTTPException) as e:
            self._resolve({"platforms": ["myspace"], "user_id": "me"})
        assert e.value.status_code == 400


def test_job_schedules_best_clips_and_reports_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_AUTO_POST_CALENDAR", str(tmp_path / "cal.json"))
    sent = []

    async def fake_upload(job_id, clip, key, user, platforms, **kw):
        if clip["video_url"] == "/bad":
            raise HTTPException(status_code=502, detail="vendor down")
        sent.append((clip["video_url"], key, user, platforms, kw["scheduled_date"]))
        return {}

    monkeypatch.setattr(app, "_upload_clip", fake_upload)
    job = {
        "status": "completed",
        "logs": app._TimedLog([]),
        "auto_post": {**VALID, "clips": 3},
        "auto_post_key": "k",
        "result": {"clips": [
            {"video_url": "/low", "predicted_score": 10},
            {"video_url": "/bad", "predicted_score": 90},
            {"video_url": "/top", "predicted_score": 95},
            {"video_url": "/mid", "predicted_score": 50},
        ]},
    }

    asyncio.run(app._auto_post_job("job1", job))

    assert [s[0] for s in sent] == ["/top", "/mid"]
    assert all(s[1:4] == ("k", "me", ["youtube", "instagram"]) for s in sent)
    first, second = (datetime.fromisoformat(s[4]) for s in sent)
    assert second - first == timedelta(hours=6)  # the failed clip still used its slot
    assert any("could not be scheduled" in line for line in job["logs"])


def test_network_errors_are_retried_until_scheduled(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_AUTO_POST_CALENDAR", str(tmp_path / "cal.json"))
    monkeypatch.setattr(app, "_AUTO_POST_RETRY_SECONDS", 0)
    calls = []

    async def flaky_upload(job_id, clip, key, user, platforms, **kw):
        calls.append(clip["video_url"])
        if len(calls) < 3:
            raise OSError("[Errno -2] Name or service not known")
        return {}

    monkeypatch.setattr(app, "_upload_clip", flaky_upload)
    job = {"status": "completed", "logs": app._TimedLog([]), "auto_post": {**VALID, "clips": 1},
           "auto_post_key": "k", "result": {"clips": [{"video_url": "/a"}]}}

    asyncio.run(app._auto_post_job("job1", job))

    assert calls == ["/a", "/a", "/a"]
    assert "scheduled for" in job["logs"][-1] and "could not" not in job["logs"][-1]


def test_job_without_key_after_restart_says_why(monkeypatch):
    monkeypatch.delenv("UPLOAD_POST_API_KEY", raising=False)
    job = {"status": "completed", "logs": app._TimedLog([]), "auto_post": VALID,
           "result": {"clips": [{"video_url": "/a"}]}}

    asyncio.run(app._auto_post_job("job1", job))

    assert "UPLOAD_POST_API_KEY" in job["logs"][-1]


class TestPostedLedger:
    def test_same_video_any_link_form_is_one_source(self):
        ids = {auto_post.source_key(u) for u in (
            " https://www.youtube.com/watch?v=LhllldUkiJU",
            "https://youtu.be/LhllldUkiJU?t=30",
            "https://www.youtube.com/shorts/LhllldUkiJU",
            "https://www.youtube.com/watch?v=LhllldUkiJU&list=PL123")}
        assert ids == {"LhllldUkiJU"}
        assert auto_post.source_key(None) is None

    def test_overlap_rule(self):
        posted = [(100.0, 140.0)]
        assert auto_post.overlaps_posted({"start": 105, "end": 138}, posted)
        assert auto_post.overlaps_posted({"start": 120, "end": 170}, posted)  # 20 of 40 s
        assert not auto_post.overlaps_posted({"start": 130, "end": 180}, posted)  # 10 of 40 s
        assert not auto_post.overlaps_posted({"start": 300, "end": 340}, posted)

    def test_ledger_round_trip(self, tmp_path):
        ledger = tmp_path / "posted.json"
        assert auto_post.posted_ranges(ledger, "vid", "yt") == []
        auto_post.record_posted(ledger, "vid", "yt", {"start": "10.5", "end": "40"})
        auto_post.record_posted(ledger, "other", "yt", {"start": 1, "end": 2})
        assert auto_post.posted_ranges(ledger, "vid", "yt") == [(10.5, 40.0)]
        assert auto_post.posted_ranges(ledger, "vid", "tt") == []  # another account
        auto_post.record_posted(ledger, None, "yt", {"start": 1, "end": 2})  # uploads: no key
        assert set(json.loads(ledger.read_text())) == {"vid@yt", "other@yt"}


def test_rerun_skips_a_moment_already_posted_and_fills_with_the_next(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_AUTO_POST_CALENDAR", str(tmp_path / "cal.json"))
    monkeypatch.setattr(app, "_POSTED_LEDGER", str(tmp_path / "posted.json"))
    auto_post.record_posted(app._POSTED_LEDGER, "LhllldUkiJU", "me",
                            {"start": 1378.58, "end": 1413.32})
    monkeypatch.setitem(app.jobs, "rerun", {"cmd": ["/opt/venv/bin/python", "-u", "main.py", "-u",
                                                   " https://youtu.be/LhllldUkiJU"]})
    sent = []

    async def fake_upload(job_id, clip, key, user, platforms, **kw):
        sent.append(clip["video_url"])
        return {}

    monkeypatch.setattr(app, "_upload_clip", fake_upload)
    job = {"status": "completed", "logs": app._TimedLog([]), "auto_post": {**VALID, "clips": 2},
           "auto_post_key": "k", "result": {"clips": [
               {"video_url": "/same", "start": 1379.0, "end": 1412.0, "predicted_score": 99},
               {"video_url": "/b", "start": 200, "end": 240, "predicted_score": 80},
               {"video_url": "/c", "start": 900, "end": 940, "predicted_score": 70},
           ]}}

    asyncio.run(app._auto_post_job("rerun", job))

    assert sent == ["/b", "/c"]
    assert any("already posted" in line for line in job["logs"])


def test_one_job_posts_to_each_channel_at_its_own_pace(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_AUTO_POST_CALENDAR", str(tmp_path / "cal.json"))
    monkeypatch.setattr(app, "_POSTED_LEDGER", str(tmp_path / "posted.json"))
    monkeypatch.setitem(app.jobs, "j", {"cmd": ["python", "-u", "main.py", "-u", "https://youtu.be/LhllldUkiJU"]})
    sent = []

    async def fake_schedule(job_id, clip, key, opts, slot):
        sent.append((opts["user_id"], clip["video_url"], slot))
        auto_post.record_posted(app._POSTED_LEDGER, "LhllldUkiJU", opts["user_id"], clip)

    monkeypatch.setattr(app, "_schedule_clip", fake_schedule)
    job = {"status": "completed", "logs": app._TimedLog([]), "auto_post_key": "k",
           "auto_post": [{**VALID, "user_id": "yt", "clips": 2, "interval_hours": 3},
                         {**VALID, "user_id": "tt", "clips": 2, "interval_hours": 24}],
           "result": {"clips": [{"video_url": "/a", "start": 0, "end": 30, "predicted_score": 90},
                                {"video_url": "/b", "start": 60, "end": 90, "predicted_score": 80}]}}

    asyncio.run(app._auto_post_job("j", job))

    by_channel = {}
    for channel, url, slot in sent:
        by_channel.setdefault(channel, []).append((url, slot))
    assert [u for u, _ in by_channel["yt"]] == ["/a", "/b"]
    assert [u for u, _ in by_channel["tt"]] == ["/a", "/b"]  # posted on YouTube, still fresh for TikTok
    (_, yt1), (_, yt2) = by_channel["yt"]
    (_, tt1), (_, tt2) = by_channel["tt"]
    assert yt2 - yt1 == timedelta(hours=3) and tt2 - tt1 == timedelta(hours=24)
    assert yt1 == tt1  # each channel starts on its own calendar
