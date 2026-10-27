import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKERS_DIR = ROOT / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import run_cycle


def test_run_cycle_fails_loudly_when_supabase_is_missing(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        run_cycle.main()
