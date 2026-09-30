"""Blotato as a posting provider (blotato.py + the auto-post dispatch in app.py).

Blotato's HTTP API is mocked at the transport; what is checked is the request
we build (upload first, then a post pointing at Blotato's copy of the clip)
and which failures are worth retrying.
"""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException

import app
import blotato

PUBLIC_URL = "https://database.blotato.io/media/clip.mp4"


def _mock(monkeypatch, post_status=201, calls=None):
    calls = [] if calls is None else calls

    def handler(request):
        calls.append(request)
        path = request.url.path
        if path.endswith("/media/uploads"):
            return httpx.Response(201, json={"presignedUrl": "https://upload.example/put",
                                             "publicUrl": PUBLIC_URL})
        if request.url.host == "upload.example":
            return httpx.Response(200)
        if path.endswith("/posts"):
            if post_status >= 400:
                return httpx.Response(post_status, json={"message": "nope"})
            return httpx.Response(201, json={"postSubmissionId": "sub-1"})
        if path.endswith("/users/me/accounts"):
            return httpx.Response(200, json={"items": [
                {"id": "51821", "platform": "youtube", "fullname": "Satoshi Minutes", "username": ""}]})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(blotato.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return calls


@pytest.fixture
def clip_file(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video-bytes")
    return f


def test_post_uploads_the_file_then_posts_blotatos_copy(monkeypatch, clip_file):
    calls = _mock(monkeypatch)

    blotato.post_video("k", "blotato:51821", str(clip_file), "youtube",
                       "A <bad> title", "desc", "2026-09-30T13:43:00Z")

    upload, put, post = calls
    assert upload.headers["blotato-api-key"] == "k"
    assert json.loads(upload.content) == {"filename": "clip.mp4"}
    assert put.content == b"video-bytes"
    body = json.loads(post.content)
    assert body["scheduledTime"] == "2026-09-30T13:43:00Z"
    assert body["post"]["accountId"] == "51821"
    assert body["post"]["content"]["mediaUrls"] == [PUBLIC_URL]
    assert body["post"]["target"] == {"targetType": "youtube", "title": "A bad title",
                                      "privacyStatus": "public", "shouldNotifySubscribers": True}


def test_tiktok_post_carries_every_field_blotato_requires(monkeypatch, clip_file):
    calls = _mock(monkeypatch)
    blotato.post_video("k", "blotato:61997", str(clip_file), "tiktok", "T", "caption")
    body = json.loads(calls[-1].content)
    assert body["post"]["content"]["platform"] == "tiktok"
    assert body["post"]["target"] == {
        "targetType": "tiktok", "privacyLevel": "PUBLIC_TO_EVERYONE", "disabledComments": False,
        "disabledDuet": False, "disabledStitch": False, "isBrandedContent": False,
        "isYourBrand": False, "isAiGenerated": False}


@pytest.mark.parametrize("status,retryable", [(422, False), (403, False), (429, True), (503, True)])
def test_only_rate_limits_and_server_errors_are_retryable(monkeypatch, clip_file, status, retryable):
    _mock(monkeypatch, post_status=status)
    with pytest.raises(blotato.BlotatoError) as e:
        blotato.post_video("k", "blotato:1", str(clip_file), "youtube", "t", "d")
    assert e.value.retryable is retryable


def test_accounts_become_picker_channels(monkeypatch):
    _mock(monkeypatch)
    assert blotato.list_accounts("k") == [
        {"channel": "blotato:51821", "label": "Satoshi Minutes", "platform": "youtube"}]


class TestSubmit:
    OPTS = {"platforms": ["youtube"], "user_id": "blotato:51821", "clips": 2, "interval_hours": 3}

    def _resolve(self, raw):
        return asyncio.run(app._resolve_auto_post(None, raw))

    def test_needs_the_server_key(self, monkeypatch):
        monkeypatch.delenv("BLOTATO_API_KEY", raising=False)
        with pytest.raises(HTTPException) as e:
            self._resolve(self.OPTS)
        assert "BLOTATO_API_KEY" in e.value.detail

    def test_one_supported_platform_per_channel(self, monkeypatch):
        monkeypatch.setenv("BLOTATO_API_KEY", "k")
        assert self._resolve({**self.OPTS, "platforms": ["tiktok"]})[0]["platforms"] == ["tiktok"]
        for bad in (["instagram"], ["youtube", "tiktok"]):
            with pytest.raises(HTTPException):
                self._resolve({**self.OPTS, "platforms": bad})

    def test_accepted_without_an_upload_post_key(self, monkeypatch):
        monkeypatch.setenv("BLOTATO_API_KEY", "k")
        opts, key = self._resolve(self.OPTS)
        assert opts["user_id"] == "blotato:51821" and key is None


def test_blotato_channel_is_scheduled_through_blotato_and_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOTATO_API_KEY", "k")
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app, "_POSTED_LEDGER", str(tmp_path / "posted.json"))
    monkeypatch.setitem(app.jobs, "j", {"cmd": ["python", "-u", "main.py", "-u",
                                                "https://youtu.be/h5TP6tAVP24"]})
    sent = []
    monkeypatch.setattr(blotato, "post_video", lambda *a: sent.append(a) or {"postSubmissionId": "x"})
    clip = {"video_url": "/videos/j/c.mp4", "start": 10, "end": 40,
            "video_title_for_youtube_short": "T", "video_description_for_instagram": "D"}
    slot = datetime(2026, 9, 30, 13, 43, tzinfo=timezone.utc)

    asyncio.run(app._schedule_clip("j", clip, "k", TestSubmit.OPTS, slot))

    key, channel, path, platform, title, text, when = sent[0]
    assert (key, channel, platform, title, text, when) == (
        "k", "blotato:51821", "youtube", "T", "D", "2026-09-30T13:43:00Z")
    assert path.endswith("j/c.mp4")
    assert app.auto_post.posted_ranges(app._POSTED_LEDGER, "h5TP6tAVP24") == [(10.0, 40.0)]


def test_a_blotato_rejection_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))

    def reject(*a):
        raise blotato.BlotatoError("Creating the post failed (422): bad title", 422)

    monkeypatch.setattr(blotato, "post_video", reject)
    clip = {"video_url": "/videos/j/c.mp4"}
    with pytest.raises(HTTPException):
        asyncio.run(app._schedule_clip("j", clip, "k", TestSubmit.OPTS,
                                       datetime(2026, 9, 30, tzinfo=timezone.utc)))
