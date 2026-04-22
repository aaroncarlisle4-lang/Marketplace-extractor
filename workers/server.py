import logging
import os
import resource
import signal
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
# Grace period between SIGTERM and SIGKILL
_SIGKILL_GRACE_SECONDS = 10


def _get_fd_count() -> int:
    """Return the number of open file descriptors for the current process."""
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except Exception:
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            return soft  # fallback: not accurate but won't crash
        except Exception:
            return -1


def _terminate_proc(proc: subprocess.Popen) -> None:
    """Escalate from SIGTERM → SIGKILL with a grace period, then reap."""
    try:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=_SIGKILL_GRACE_SECONDS)
            logger.info("Subprocess exited cleanly after SIGTERM (pid=%d)", proc.pid)
            return
        except subprocess.TimeoutExpired:
            logger.warning(
                "Subprocess did not exit after SIGTERM — sending SIGKILL (pid=%d)", proc.pid
            )
    except ProcessLookupError:
        return  # already gone
    except Exception as exc:
        logger.warning("SIGTERM failed (pid=%d): %s", proc.pid, exc)

    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception as exc:
        logger.warning("SIGKILL failed (pid=%d): %s", proc.pid, exc)

    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _close_proc_pipes(proc: subprocess.Popen) -> None:
    """Explicitly close any pipes opened on the subprocess to release file descriptors."""
    for attr in ("stdin", "stdout", "stderr"):
        pipe = getattr(proc, attr, None)
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass


def run_vinted_cycle():
    if not _scraper_lock.acquire(blocking=False):
        logger.info("Skipping Vinted cycle — another scraper is already running")
        return
    proc = None
    fd_before = _get_fd_count()
    try:
        logger.info(
            "Starting Vinted cycle (subprocess, timeout=%ds, open_fds=%d)",
            _SUBPROCESS_TIMEOUT,
            fd_before,
        )
        proc = subprocess.Popen(
            [sys.executable, "/app/run_cycle.py"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
            close_fds=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=_SUBPROCESS_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Vinted cycle timed out after %ds — terminating subprocess (pid=%d)",
                _SUBPROCESS_TIMEOUT,
                proc.pid,
            )
            _terminate_proc(proc)
            # Drain any remaining output so pipes are fully closed
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = b"", b""
            _last_run["vinted_error"] = f"timeout after {_SUBPROCESS_TIMEOUT}s"
            return

        if proc.returncode == 0:
            _last_run["vinted"] = datetime.now(timezone.utc).isoformat()
            _last_run["vinted_error"] = None
            logger.info("Vinted cycle completed successfully")
            if stdout:
                logger.info("Output: %s", stdout.decode(errors="replace")[-1000:])
        else:
            error = stderr.decode(errors="replace")[-500:] if stderr else f"exit {proc.returncode}"
            _last_run["vinted_error"] = error
            logger.error("Vinted cycle failed (exit %d): %s", proc.returncode, error)
            if stdout:
                logger.debug("stdout: %s", stdout.decode(errors="replace")[-500:])

    except OSError as e:
        # Catches BlockingIOError / resource exhaustion at the OS level
        _last_run["vinted_error"] = str(e)
        logger.error("Vinted cycle OS error (resource exhaustion?): %s", e, exc_info=True)
    except Exception as e:
        _last_run["vinted_error"] = str(e)
        logger.exception("Vinted cycle error: %s", e)
    finally:
        if proc is not None:
            _close_proc_pipes(proc)
            # Ensure the process is fully reaped even if communicate() was skipped
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        fd_after = _get_fd_count()
        leaked = fd_after - fd_before
        if leaked > 10:
            logger.warning(
                "Possible FD leak after Vinted cycle: before=%d after=%d delta=+%d",
                fd_before,
                fd_after,
                leaked,
            )
        else:
            logger.info(
                "Vinted cycle done: open_fds before=%d after=%d delta=%+d",
                fd_before,
                fd_after,
                leaked,
            )
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
