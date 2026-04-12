import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import Response

logger = logging.getLogger("scraper-service")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

# Global lock: only one scraper cycle at a time (Chrome memory constraint)
_scraper_lock = threading.Lock()

_last_run = {
    "vinted": None,
    "vinted_error": None,
}


def run_vinted_cycle():
    if not _scraper_lock.acquire(blocking=False):
        logger.info("Skipping Vinted cycle — another scraper is already running")
        return
    try:
        logger.info("Starting Vinted cycle")
        from run_cycle import main as vinted_main
        vinted_main()
        _last_run["vinted"] = datetime.now(timezone.utc).isoformat()
        _last_run["vinted_error"] = None
        logger.info("Vinted cycle completed")
    except Exception as e:
        _last_run["vinted_error"] = str(e)
        logger.exception("Vinted cycle failed: %s", e)
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
