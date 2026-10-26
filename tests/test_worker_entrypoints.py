import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKERS_DIR = ROOT / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import run_cycle


def test_run_cycle_exits_cleanly_when_env_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("CONVEX_SITE_URL", raising=False)
    monkeypatch.delenv("INGEST_SHARED_SECRET", raising=False)

    exit_code = run_cycle.main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "skipped" in captured.out.lower() or "missing" in captured.out.lower()
