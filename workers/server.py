import logging
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

logger = logging.getLogger("scraper-service")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

# Global lock: only one Chrome process at a time
_scraper_lock = threading.Lock()

_last_run = {
    "vinted": None,
    "vinted_error": None,
}

# Hard timeout: kill Chrome + scraper if it overruns
_SUBPROCESS_TIMEOUT = int(os.getenv("SUBPROCESS_TIMEOUT_SECONDS", "150"))


def run_vinted_cycle():
    if not _scraper_lock.acquire(blocking=False):
        logger.info("Skipping Vinted cycle — another scraper is already running")
        return
    proc = None
    try:
        logger.info("Starting Vinted cycle (subprocess, timeout=%ds)", _SUBPROCESS_TIMEOUT)
        proc = subprocess.Popen(
            [sys.executable, "/app/run_cycle.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout, stderr = proc.communicate(timeout=_SUBPROCESS_TIMEOUT)
        if proc.returncode == 0:
            _last_run["vinted"] = datetime.now(timezone.utc).isoformat()
            _last_run["vinted_error"] = None
            logger.info("Vinted cycle completed")
            if stdout:
                logger.info("Output: %s", stdout.decode(errors="replace")[-1000:])
        else:
            error = stderr.decode(errors="replace")[-500:] if stderr else f"exit {proc.returncode}"
            _last_run["vinted_error"] = error
            logger.error("Vinted cycle failed (exit %d): %s", proc.returncode, error)
    except subprocess.TimeoutExpired:
        logger.warning("Vinted cycle timed out after %ds — killing Chrome", _SUBPROCESS_TIMEOUT)
        if proc:
            proc.kill()
            proc.communicate()
        _last_run["vinted_error"] = f"timeout after {_SUBPROCESS_TIMEOUT}s"
    except Exception as e:
        _last_run["vinted_error"] = str(e)
        logger.exception("Vinted cycle error: %s", e)
    finally:
        _scraper_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    interval = int(os.getenv("VINTED_INTERVAL_MINUTES", "10"))
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_vinted_cycle,
        "interval",
        minutes=interval,
        id="vinted",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    scheduler.start()
    logger.info("Scheduler started: Vinted every %d minutes", interval)
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {
        "status": "ok",
        "utc": datetime.now(timezone.utc).isoformat(),
        "last_vinted": _last_run["vinted"],
        "vinted_error": _last_run["vinted_error"],
    }
