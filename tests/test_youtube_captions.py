"""YouTube ASR captions -> word-level transcript (youtube_captions.py).

The pipeline cuts clips and times karaoke words from this, so the checks are
on the timing and on never picking a machine-translated track.
"""
import youtube_captions as yc


def _json3(words_per_event):
    """[(event_start_ms, event_dur_ms, [(offset_ms, text), ...]), ...] -> json3."""
    events = []
    for start, dur, segs in words_per_event:
        events.append({"tStartMs": start, "dDurationMs": dur,
                       "segs": [{"utf8": t, "tOffsetMs": o} for o, t in segs]})
        events.append({"tStartMs": start + 10, "aAppend": 1, "segs": [{"utf8": "\n"}]})
    return {"events": events}


def _many_words(n, start_ms=0, step_ms=300):
    return [(start_ms, n * step_ms, [(i * step_ms, f"w{i}") for i in range(n)])]


class TestAsrTrack:
    def test_prefers_orig_track_over_translations(self):
        info = {"language": "en", "automatic_captions": {
            "es": [{"ext": "json3", "url": "translated"}],
            "en-orig": [{"ext": "json3", "url": "asr"}],
            "en": [{"ext": "json3", "url": "en-plain"}],
        }}
        assert yc.asr_track(info) == ("asr", "en")

    def test_plain_spoken_language_when_no_orig_key(self):
        info = {"language": "es-419", "automatic_captions": {
            "es-419": [{"ext": "vtt", "url": "x"}, {"ext": "json3", "url": "asr"}]}}
        assert yc.asr_track(info) == ("asr", "es")

    def test_auto_dubbed_video_uses_the_original_audio_language(self):
        # Seen on a real English podcast: 19 "-orig" tracks, one per dub,
        # and no info["language"] in the unprocessed info.
        info = {"formats": [
                    {"language": "ar", "language_preference": -1},
                    {"language": "en-US", "language_preference": 10},
                    {"language": "de-DE", "language_preference": -1}],
                "automatic_captions": {
                    "ar-orig": [{"ext": "json3", "url": "arabic"}],
                    "en-orig": [{"ext": "json3", "url": "english"}],
                    "de-DE-orig": [{"ext": "json3", "url": "german"}]}}
        assert yc.asr_track(info) == ("english", "en")

    def test_unknown_language_with_several_tracks_means_transcribe_locally(self):
        info = {"automatic_captions": {"ar-orig": [{"ext": "json3", "url": "a"}],
                                       "en-orig": [{"ext": "json3", "url": "e"}]}}
        assert yc.asr_track(info) == (None, None)

    def test_unknown_language_with_one_track_uses_it(self):
        info = {"automatic_captions": {"en-orig": [{"ext": "json3", "url": "e"}]}}
        assert yc.asr_track(info) == ("e", "en")

    def test_none_without_asr_track(self):
        info = {"language": "en", "subtitles": {"en": [{"ext": "json3", "url": "human"}]},
                "automatic_captions": {"fr": [{"ext": "json3", "url": "translated"}]}}
        assert yc.asr_track(info) == (None, None)


class TestTranscript:
    def test_words_are_timed_and_space_prefixed(self):
        data = _json3(_many_words(25, start_ms=1000))
        t = yc.transcript_from_json3(data, "en")
        words = [w for s in t["segments"] for w in s["words"]]
        assert len(words) == 25
        assert words[0] == {"word": " w0", "start": 1.0, "end": 1.3}
        assert all(a["end"] <= b["start"] for a, b in zip(words, words[1:]))
        assert t["language"] == "en"

    def test_last_word_before_a_pause_does_not_stretch_across_it(self):
        data = _json3(_many_words(20) + [(60000, 2000, [(0, "later")])])
        words = [w for s in yc.transcript_from_json3(data, "en")["segments"] for w in s["words"]]
        before_pause = words[-2]
        assert before_pause["end"] - before_pause["start"] <= yc.MAX_WORD_SECONDS

    def test_segments_split_on_gaps(self):
        data = _json3(_many_words(20) + [(60000, 2000, [(0, "later"), (300, "words")])])
        segments = yc.transcript_from_json3(data, "en")["segments"]
        assert segments[-1]["text"] == " later words"
        assert segments[-1]["start"] == 60.0

    def test_sound_tags_are_dropped(self):
        data = _json3([(0, 900, [(0, "[Music]")])] + _many_words(20, start_ms=1000))
        words = [w["word"] for s in yc.transcript_from_json3(data, "en")["segments"]
                 for w in s["words"]]
        assert " [Music]" not in words

    def test_too_few_words_means_transcribe_locally(self):
        assert yc.transcript_from_json3(_json3(_many_words(5)), "en") is None
        assert yc.transcript_from_json3({"events": []}, "en") is None


def test_off_switch(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CAPTIONS", "0")
    assert not yc.enabled()
    monkeypatch.delenv("YOUTUBE_CAPTIONS")
    assert yc.enabled()
