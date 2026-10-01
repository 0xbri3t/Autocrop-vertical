"""Blotato as a posting provider (https://backend.blotato.com/v2).

A Blotato channel is one connected account (one YouTube channel, one TikTok,
...), addressed in auto-post as ``blotato:<accountId>`` so it can sit in the
same channel picker as the Upload-Post profiles. Posting is two steps:
Blotato only publishes media hosted on its own domain, so the local clip is
PUT to a presigned upload URL first, then the post references the returned
public URL.
"""
import os

import httpx

API = "https://backend.blotato.com/v2"
CHANNEL_PREFIX = "blotato:"
TIMEOUT = 120.0


class BlotatoError(Exception):
    """Blotato answered with an error; the message is Blotato's own.
    ``retryable`` for rate limits and server errors, not for rejections."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.retryable = status is None or status == 429 or status >= 500


def api_key():
    return os.environ.get("BLOTATO_API_KEY", "").strip()


def is_channel(channel: str) -> bool:
    return bool(channel) and channel.startswith(CHANNEL_PREFIX)


def account_id(channel: str) -> str:
    return channel[len(CHANNEL_PREFIX):]


def _check(resp: httpx.Response, what: str) -> dict:
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message") or resp.text
        except ValueError:
            message = resp.text
        raise BlotatoError(f"{what} failed ({resp.status_code}): {message[:300]}", resp.status_code)
    return resp.json() if resp.content else {}  # PATCH answers 204 with no body


def list_accounts(key: str) -> list:
    """``[{channel, label, platform}]`` for every connected account."""
    with httpx.Client(timeout=30.0) as client:
        data = _check(client.get(f"{API}/users/me/accounts",
                                 headers={"blotato-api-key": key}), "Listing accounts")
    return [{"channel": f"{CHANNEL_PREFIX}{a['id']}",
             "label": a.get("fullname") or a.get("username") or a["id"],
             "platform": a.get("platform")}
            for a in data.get("items") or []]


def _youtube_title(title: str) -> str:
    return (title or "Short").replace("<", "").replace(">", "")[:100]


PLATFORMS = ("youtube", "tiktok", "instagram")


def _target(platform, title, privacy):
    """Blotato's per-platform ``target``: every field it marks required."""
    if platform == "youtube":
        return {"targetType": "youtube", "title": _youtube_title(title),
                "privacyStatus": privacy, "shouldNotifySubscribers": privacy == "public"}
    if platform == "tiktok":
        # Edited clips of real footage: not AI-generated, not branded content.
        return {"targetType": "tiktok",
                "privacyLevel": "PUBLIC_TO_EVERYONE" if privacy == "public" else "SELF_ONLY",
                "disabledComments": False, "disabledDuet": False, "disabledStitch": False,
                "isBrandedContent": False, "isYourBrand": False, "isAiGenerated": False}
    if platform == "instagram":
        # A Reel, also shown on the profile grid. Instagram has no private
        # mode, so ``privacy`` does not apply.
        return {"targetType": "instagram", "mediaType": "reel", "shareToFeed": True}
    raise BlotatoError(f"Posting to {platform} through Blotato is not supported here.", 400)


def post_video(key, channel, file_path, platform, title, text, scheduled_iso=None,
               privacy="public"):
    """Upload ``file_path`` and publish (or schedule, ISO 8601 UTC) it on the
    account behind ``channel``. Blocking; returns Blotato's post response."""
    headers = {"blotato-api-key": key}
    with httpx.Client(timeout=TIMEOUT) as client:
        upload = _check(client.post(f"{API}/media/uploads", headers=headers,
                                    json={"filename": os.path.basename(file_path)}),
                        "Creating the upload")
        with open(file_path, "rb") as f:
            put = client.put(upload["presignedUrl"], content=f.read(),
                             headers={"Content-Type": "video/mp4"})
        if put.status_code >= 400:
            raise BlotatoError(f"Uploading the clip failed ({put.status_code}): {put.text[:300]}",
                               put.status_code)

        target = _target(platform, title, privacy)
        body = {"post": {"accountId": account_id(channel),
                         "content": {"text": text or title or "", "platform": platform,
                                     "mediaUrls": [upload["publicUrl"]]},
                         "target": target}}
        if scheduled_iso:
            body["scheduledTime"] = scheduled_iso
        result = _check(client.post(f"{API}/posts", headers=headers, json=body), "Creating the post")
        if scheduled_iso:
            _enforce_time(client, headers, upload["publicUrl"], scheduled_iso)
        return result


def _same_instant(a: str, b: str) -> bool:
    from datetime import datetime
    return datetime.fromisoformat(a.replace("Z", "+00:00")) == datetime.fromisoformat(b.replace("Z", "+00:00"))


def _enforce_time(client, headers, media_url, scheduled_iso):
    """Read back the stored schedule and move it if Blotato shifted it.

    29-sep-2026: two YouTube posts created with scheduledTime 13:43Z and
    14:43Z were stored at 11:43Z and 12:43Z (the account's UTC+2 offset) and
    one went out two hours early; a later identical request was stored
    correctly. The upload URL is unique per post, so it identifies the
    schedule without guessing.
    """
    items = _check(client.get(f"{API}/schedules", headers=headers, params={"limit": 100}),
                   "Reading back the schedule").get("items") or []
    for item in items:
        media = (((item.get("draft") or {}).get("content") or {}).get("mediaUrls") or [])
        if media_url in media and not _same_instant(item.get("scheduledAt") or scheduled_iso,
                                                    scheduled_iso):
            print(f"⚠️ Blotato stored {item.get('scheduledAt')} instead of {scheduled_iso}; moving it.")
            _check(client.patch(f"{API}/schedules/{item['id']}", headers=headers,
                                json={"patch": {"scheduledTime": scheduled_iso}}),
                   "Correcting the scheduled time")
