import importlib.util
import json
import sys
from pathlib import Path


SCRIPTS = Path(__file__).parent


def load_sync():
    path = SCRIPTS / "run_local_funnel_sync.py"
    spec = importlib.util.spec_from_file_location("run_local_funnel_sync", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_local_funnel_sync"] = module
    spec.loader.exec_module(module)
    return module


def test_latest_nonempty_skips_newer_empty_artifact(tmp_path):
    mod = load_sync()
    older = tmp_path / "rows_1.json"
    newer = tmp_path / "rows_2.json"
    older.write_text(json.dumps([{"job_url": "https://example.com/1"}]))
    newer.write_text("[]")
    older.touch()
    newer.touch()
    newer_mtime = older.stat().st_mtime + 10
    import os
    os.utime(newer, (newer_mtime, newer_mtime))
    assert mod.latest_nonempty(str(tmp_path / "rows_*.json")) == older


def test_sync_imports_each_latest_source_and_reports_failures(tmp_path):
    mod = load_sync()
    data = tmp_path / "data"
    good = data / "good_1.json"
    good.parent.mkdir(parents=True)
    good.write_text(json.dumps([{
        "source": "telegram", "job_title": "Product Lead", "company": "A",
        "job_url": "https://t.me/jobs/1", "fit_score": "90",
    }]))
    broken = data / "broken_1.json"
    broken.write_text("not-json")
    report = mod.sync(
        db=tmp_path / "funnel.sqlite3",
        patterns={"good": str(data / "good_*.json"), "broken": str(data / "broken_*.json")},
    )
    assert report["status"] == "degraded"
    assert report["sources"]["good"]["accepted"] == 1
    assert report["sources"]["broken"]["status"] == "error"
    assert report["summary"]["jobs"] == 1
