"""Self-host run history (app.local_history).

It lists every job on disk, so the two things that must hold are that cloud
mode never answers it (it would list every account's jobs) and that it only
shows runs that are finished and still on disk.
"""
import asyncio
import json
import os

import pytest
from fastapi import HTTPException

import app


def _write(path, body="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(body)


def _finished_job(job_id, title="My_Video", clips=2):
    shorts = [{"video_title_for_youtube_short": f"t{i}",
               "video_url": f"/videos/{job_id}/{title}_clip_{i + 1}.mp4"} for i in range(clips)]
    _write(f"output/{job_id}/{title}_metadata.json", json.dumps({"shorts": shorts}))


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("output", exist_ok=True)
    monkeypatch.setattr(app, "jobs", {})
    monkeypatch.setattr(app, "BILLING_ENABLED", False)
    return tmp_path


def _history():
    return asyncio.run(app.local_history())


def test_lists_finished_jobs_with_their_clips(workdir):
    _finished_job("job1")

    d = _history()

    assert [p["job_id"] for p in d["projects"]] == ["job1"]
    assert d["projects"][0]["title"] == "My Video"
    assert [(v["clip_index"], v["title"], v["view_url"]) for v in d["videos"]] == [
        (0, "t0", "/videos/job1/My_Video_clip_1.mp4"),
        (1, "t1", "/videos/job1/My_Video_clip_2.mp4"),
    ]


def test_skips_jobs_still_rendering(workdir):
    _finished_job("job1")
    _write("output/job1/.resume.json", "{}")

    assert _history()["projects"] == []


def test_skips_jobs_whose_files_were_swept(workdir):
    app.jobs["gone"] = {"status": "completed", "result": {"clips": [{"video_url": "/videos/gone/a.mp4"}]}}

    assert _history()["videos"] == []


def test_cloud_mode_answers_404(workdir, monkeypatch):
    _finished_job("job1")
    monkeypatch.setattr(app, "BILLING_ENABLED", True)

    with pytest.raises(HTTPException) as e:
        _history()
    assert e.value.status_code == 404
