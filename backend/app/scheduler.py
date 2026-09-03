"""งานรายสัปดาห์ — ยิงหลัง CFTC เผยแพร่ (ศุกร์ 15:30 ET)"""
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import text

from .db import engine
from .etl import ingest_cot, rebuild_derived, snap_weekly_price
from .main import evaluate_and_store

log = logging.getLogger(__name__)


def _run(name: str, fn):
    with engine.begin() as c:
        jid = c.execute(text(
            "INSERT INTO job_run (job_name) VALUES (:n)"), {"n": name}).lastrowid
    try:
        n = fn()
        status, msg = "SUCCESS", None
    except Exception as e:                      # noqa: BLE001
        n, status, msg = 0, "FAILED", str(e)[:2000]
        log.exception("งาน %s ล้มเหลว", name)
    with engine.begin() as c:
        c.execute(text("""UPDATE job_run SET status=:s, rows_written=:n,
                          message=:m, finished_at=NOW() WHERE id=:i"""),
                  {"s": status, "n": n, "m": msg, "i": jid})


def weekly():
    _run("ingest_cot", ingest_cot)
    _run("snap_weekly_price", snap_weekly_price)
    _run("rebuild_derived", rebuild_derived)
    _run("evaluate_signals", evaluate_and_store)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sched = BlockingScheduler(timezone="Asia/Bangkok")
    sched.add_job(weekly, "cron", day_of_week="sat", hour=5, minute=0)
    sched.start()
