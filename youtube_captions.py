"""YouTube's own speech-recognition captions as a word-level transcript.

Every YouTube video with speech has an auto-caption track whose json3 form
times each word, which is what the clip picker, the cuts and the karaoke
subtitles need. Reading it takes one request instead of a CPU transcription
(~16 min for an hour of audio on a laptop). Only the ASR track is used: human
captions are better text but carry no per-word timing.

On by default for YouTube URLs; ``YOUTUBE_CAPTIONS=0`` turns it off. Any
problem (no ASR track, too few words, a failed request) returns None and the
job transcribes locally as before.
"""
import json
import os
import re

MIN_WORDS = 20
MAX_WORD_SECONDS = 1.5
SEGMENT_GAP_SECONDS = 1.0
SEGMENT_MAX_SECONDS = 10.0
_TAG = re.compile(r"^\[[^\]]*\]$")  # [Music], [Applause]


def enabled() -> bool:
    return os.environ.get("YOUTUBE_CAPTIONS", "1").strip() != "0"


def spoken_language(info: dict) -> str:
    """The video's original spoken language, or "" when unknown.

    The unprocessed info yt-dlp hands the downloader has no ``language``;
    the original audio track is the format with the highest
    ``language_preference`` (10 for "original", -1 for YouTube's auto-dubs).
    """
    if info.get("language"):
        return info["language"]
    best = None
    for fmt in info.get("formats") or []:
        lang, pref = fmt.get("language"), fmt.get("language_preference")
        if lang and pref is not None and (best is None or pref > best[0]):
            best = (pref, lang)
    return best[1] if best else ""


def asr_track(info: dict):
    """``(json3_url, language)`` of the video's ASR caption track, or ``(None, None)``.

    ``<lang>-orig`` is the ASR track itself; every other key in
    ``automatic_captions`` is a machine translation of it and never used.
    Without the ``-orig`` key, the plain code of the spoken language is the
    ASR track. An auto-dubbed video has one ``-orig`` track per dub, so
    with the spoken language unknown only a lone ``-orig`` track is trusted:
    picking the first one read an English podcast as Arabic.
    """
    auto = info.get("automatic_captions") or {}
    spoken = spoken_language(info)
    base = spoken.split("-")[0]
    keys = [f"{spoken}-orig", f"{base}-orig", spoken, base] if spoken else []
    orig = [k for k in auto if k.endswith("-orig")]
    if not spoken and len(orig) == 1:
        keys = orig
    for key in keys:
        fmt = next((f for f in auto.get(key) or [] if f.get("ext") == "json3"), None)
        if fmt and fmt.get("url"):
            return fmt["url"], key.removesuffix("-orig").split("-")[0]
    return None, None


def _words(data: dict) -> list:
    words = []
    for ev in data.get("events") or []:
        segs = ev.get("segs")
        if not segs:
            continue
        ev_start = (ev.get("tStartMs") or 0) / 1000.0
        ev_end = ev_start + (ev.get("dDurationMs") or 0) / 1000.0
        for seg in segs:
            text = (seg.get("utf8") or "").strip()
            if not text or _TAG.match(text):
                continue
            start = ev_start + (seg.get("tOffsetMs") or 0) / 1000.0
            words.append({"word": f" {text}", "start": start, "ev_end": ev_end})
    words.sort(key=lambda w: w["start"])
    for word, nxt in zip(words, words[1:] + [None]):
        end = min(word["ev_end"], word["start"] + MAX_WORD_SECONDS)
        if nxt is not None:
            end = min(end, nxt["start"])
        word["end"] = max(end, word["start"] + 0.05)
        del word["ev_end"]
    return words


def _segments(words: list) -> list:
    segments, current = [], []
    for word in words:
        if current and (word["start"] - current[-1]["end"] > SEGMENT_GAP_SECONDS
                        or word["end"] - current[0]["start"] > SEGMENT_MAX_SECONDS):
            segments.append(current)
            current = []
        current.append(word)
    if current:
        segments.append(current)
    return [{"start": ws[0]["start"], "end": ws[-1]["end"],
             "text": "".join(w["word"] for w in ws), "words": ws} for ws in segments]


def transcript_from_json3(data, language: str):
    """json3 ASR captions -> the transcript shape transcribe_media returns,
    or None when there are too few words to trust."""
    if isinstance(data, (bytes, str)):
        data = json.loads(data)
    words = _words(data)
    if len(words) < MIN_WORDS:
        return None
    segments = _segments(words)
    return {"text": "".join(s["text"] for s in segments).strip(),
            "language": language, "segments": segments}
