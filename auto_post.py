"""Self-host auto-post: schedule a finished job's best clips on Upload-Post.

Each channel (Upload-Post profile) has its own calendar in
``output/.autopost.json``: three videos for one channel finishing together
still post one clip every N hours, while five channels post in parallel.
The calendar only remembers the last slot handed out per channel.
"""
import json
import re
import threading
from datetime import datetime, timedelta, timezone

PLATFORMS = ("tiktok", "instagram", "youtube")
FIRST_POST_DELAY = timedelta(minutes=10)
MAX_CLIPS = 15
MIN_INTERVAL_HOURS = 0.5
MAX_INTERVAL_HOURS = 48
MAX_CHANNELS = 10

_calendar_lock = threading.Lock()


def parse_options(raw):
    """Validate the ``auto_post`` field of /api/process.

    Returns None when the field is absent, else a list with one
    ``{"platforms", "user_id", "clips", "interval_hours"}`` per channel: a job
    can post to a brand's YouTube and TikTok, each at its own pace. A single
    object is one channel. Raises ValueError with a message fit for a 400.
    """
    if raw in (None, "", {}, []):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"auto_post is not valid JSON: {e}") from e
    channels = [raw] if isinstance(raw, dict) else raw
    if not isinstance(channels, list) or not all(isinstance(c, dict) for c in channels):
        raise ValueError("auto_post must be an object or a list of objects")
    if len(channels) > MAX_CHANNELS:
        raise ValueError(f"auto_post takes at most {MAX_CHANNELS} channels")
    parsed = [_parse_channel(c) for c in channels]
    ids = [c["user_id"] for c in parsed]
    if len(set(ids)) != len(ids):
        raise ValueError("auto_post lists the same channel twice")
    return parsed


def as_list(opts):
    """A job's auto_post as a list: resume manifests written before
    multi-channel posting hold a single object."""
    if not opts:
        return None
    return [opts] if isinstance(opts, dict) else opts


def _parse_channel(raw):
    platforms = raw.get("platforms")
    if (not isinstance(platforms, list) or not platforms
            or any(p not in PLATFORMS for p in platforms)):
        raise ValueError(f"auto_post.platforms must be a non-empty list of {', '.join(PLATFORMS)}")
    profile = raw.get("user_id")
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("auto_post.user_id must name the channel to post to")
    try:
        clips = int(raw.get("clips", 3))
        hours = float(raw.get("interval_hours", 3))
    except (TypeError, ValueError) as e:
        raise ValueError("auto_post.clips and auto_post.interval_hours must be numbers") from e
    if not 1 <= clips <= MAX_CLIPS:
        raise ValueError(f"auto_post.clips must be between 1 and {MAX_CLIPS}")
    if not MIN_INTERVAL_HOURS <= hours <= MAX_INTERVAL_HOURS:
        raise ValueError(
            f"auto_post.interval_hours must be between {MIN_INTERVAL_HOURS} and {MAX_INTERVAL_HOURS}")
    return {"platforms": sorted(set(platforms)), "user_id": profile.strip(),
            "clips": clips, "interval_hours": hours}


def _span(clip):
    try:
        return float(clip.get("start") or 0), float(clip.get("end") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0


def overlaps_posted(clip, posted, ratio=0.5):
    """True when the clip shares at least ``ratio`` of the shorter span with a
    range already posted from the same source (same rule as dedupe_overlapping)."""
    s, e = _span(clip)
    for ps, pe in posted:
        shorter = max(1e-6, min(e - s, pe - ps))
        if min(e, pe) - max(s, ps) >= ratio * shorter:
            return True
    return False


def pick_clips(clips, n, posted=()):
    """Indices of the ``n`` best rendered clips by predicted_score, skipping
    any that repeat a moment already posted from this source."""
    def score(i):
        try:
            return float(clips[i].get("predicted_score") or 0)
        except (TypeError, ValueError):
            return 0.0
    fresh = [i for i, c in enumerate(clips)
             if c.get("video_url") and not overlaps_posted(c, posted)]
    return sorted(fresh, key=lambda i: -score(i))[:n]


_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")


def source_key(url):
    """The YouTube video id for a URL (so every link form of one video maps to
    one key), else the stripped URL; None without one (uploads)."""
    if not url:
        return None
    m = _YT_ID.search(url)
    return m.group(1) if m else url.strip()


def _ledger_key(source, channel):
    """One entry per source video AND channel: the rule is "never the same
    moment twice on one account", so a clip on YouTube can still go to TikTok."""
    return f"{source}@{channel}"


def posted_ranges(ledger_path, source, channel):
    """``[(start, end), ...]`` of ``source`` already posted on ``channel``."""
    if not source:
        return []
    try:
        with open(ledger_path) as f:
            return [tuple(r) for r in json.load(f).get(_ledger_key(source, channel), [])]
    except FileNotFoundError:
        return []
    except (ValueError, TypeError, AttributeError) as e:
        print(f"⚠️ Posted-clips ledger {ledger_path} unreadable ({e}); treating it as empty.")
        return []


def record_posted(ledger_path, source, channel, clip):
    """Remember that this clip's span of ``source`` has been posted on ``channel``."""
    if not source:
        return
    with _calendar_lock:
        try:
            with open(ledger_path) as f:
                ledger = json.load(f)
        except (FileNotFoundError, ValueError):
            ledger = {}
        ledger.setdefault(_ledger_key(source, channel), []).append(list(_span(clip)))
        with open(ledger_path, "w") as f:
            json.dump(ledger, f)


def claim_slots(calendar_path, channel, count, interval_hours, now=None):
    """Reserve ``count`` posting times ``interval_hours`` apart on ``channel``'s
    calendar, after the last slot any earlier job took there, and never
    sooner than FIRST_POST_DELAY."""
    if count <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    step = timedelta(hours=interval_hours)
    earliest = (now + FIRST_POST_DELAY).replace(second=0, microsecond=0)
    with _calendar_lock:
        calendar = {}
        try:
            with open(calendar_path) as f:
                calendar = json.load(f)
            if not isinstance(calendar, dict):
                raise TypeError(f"expected an object, got {type(calendar).__name__}")
        except FileNotFoundError:
            pass
        except (ValueError, TypeError) as e:
            print(f"⚠️ Auto-post calendar {calendar_path} unreadable ({e}); starting a new one.")
            calendar = {}
        start = earliest
        if calendar.get(channel):
            try:
                start = max(earliest, datetime.fromisoformat(calendar[channel]) + step)
            except (TypeError, ValueError) as e:
                print(f"⚠️ Auto-post calendar entry for {channel!r} unreadable ({e}); ignoring it.")
        slots = [start + i * step for i in range(count)]
        calendar[channel] = slots[-1].isoformat()
        with open(calendar_path, "w") as f:
            json.dump(calendar, f)
    return slots
