"""Tiger Data (Timescale Cloud) sink: 10 Hz env readings -> hypertable, scans -> plain table.

Readings are buffered on the sink thread and shipped with COPY every `flush_s` (psycopg 3),
scans are upserted by id. Offline: the buffer is a bounded deque, oldest readings drop first,
and we reconnect with backoff. No connection string = disabled.

Env:  TIGERDATA_URL  postgres://tsdbadmin:...@<id>.tsdb.cloud.timescale.com:<port>/tsdb?sslmode=require
Schema: plantbot/integrations/sql/tigerdata_schema.sql
    python -m plantbot.integrations.tigerdata --init-schema     # applies it (idempotent)
    python -m plantbot.integrations.tigerdata --demo-queries    # runs the showcase queries
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import re
import time
from pathlib import Path

from plantbot.integrations.sinks import Sink

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent / "sql"
READING_COLS = ("time", "rover_id", "mq2_v", "gas_ohms", "temp_c", "rh", "pressure_hpa",
                "voc_top", "voc_bottom", "warm", "sniffing")
SCAN_COLS = ("scan_id", "time", "rover_id", "plant_id", "mode", "label", "p_healthy", "health",
             "status", "voc_stress", "mq2_ratio", "gas_ratio", "diff_ratio", "temp_c", "rh",
             "backend", "infer_ms", "record")


def _ts(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(float(t), dt.timezone.utc)


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def reading_row(s: dict) -> tuple:
    return (_ts(s["t"]), s["rover_id"], _f(s.get("mq2_v")), _f(s.get("gas_ohms")), _f(s.get("temp_c")),
            _f(s.get("rh")), _f(s.get("pressure_hpa")), _f(s.get("voc_top")), _f(s.get("voc_bottom")),
            s.get("warm"), s.get("sniffing"))


def scan_row(rec: dict) -> tuple:
    slim = {k: v for k, v in rec.items() if k not in ("image_path",)}
    return (str(rec["id"]), _ts(rec.get("t") or time.time()), rec.get("rover_id"), rec.get("plant_id"),
            rec.get("mode"), rec.get("label"), _f(rec.get("p_healthy")), _f(rec.get("health")),
            rec.get("status"), _f(rec.get("voc_stress")), _f(rec.get("mq2_ratio")), _f(rec.get("gas_ratio")),
            _f(rec.get("diff_ratio")), _f(rec.get("temp_c")), _f(rec.get("rh")), rec.get("backend"),
            _f(rec.get("infer_ms")), json.dumps(slim, default=str))


def split_sql(text: str) -> list[str]:
    """Split a .sql file on top-level ';' (respects quotes, $$ bodies and -- comments).
    Needed because continuous aggregates can't be created inside a multi-statement transaction."""
    out, buf, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and text[j + 1:j + 2] == "'":
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            buf.append(text[i:j + 1])
            i = j + 1
            continue
        m = re.match(r"\$[A-Za-z_]*\$", text[i:])
        if m:
            tag = m.group(0)
            j = text.find(tag, i + len(tag))
            j = n if j < 0 else j + len(tag)
            buf.append(text[i:j])
            i = j
            continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(c)
        i += 1
    stmt = "".join(buf).strip()
    if stmt:
        out.append(stmt)
    return out


class TigerDataSink(Sink):
    name = "tigerdata"
    queue_size = 5000

    def __init__(self, cfg: dict | None = None, rover_id: str = "rover-1", connect=None):
        cfg = cfg or {}
        self.url = os.getenv("TIGERDATA_URL") or ""
        self.rover_id = rover_id
        self.flush_s = float(cfg.get("flush_s", 2))
        self.batch_max = int(cfg.get("batch_max", 2000))
        self.readings: collections.deque = collections.deque(maxlen=int(cfg.get("buffer_max", 6000)))
        self.scans: collections.deque = collections.deque(maxlen=200)
        self.wants_readings = bool(cfg.get("readings", True))
        self._connect = connect  # tests inject a fake; default is psycopg.connect
        self.conn = None
        self._retry_at = 0.0
        self._backoff = 1.0
        self.sent_readings = 0
        self.sent_scans = 0
        self.enabled = bool(self.url) and cfg.get("enabled", True)
        if self.enabled and self._connect is None:
            try:
                import psycopg  # noqa: F401
            except ImportError:
                log.warning("Tiger Data: TIGERDATA_URL set but psycopg is not installed (pip install 'psycopg[binary]')")
                self.enabled = False
        if not self.enabled:
            log.info("Tiger Data sink disabled (set TIGERDATA_URL to enable)")

    # ---- sink API
    def on_reading(self, sample: dict) -> None:
        self.readings.append(reading_row(sample))

    def on_scan(self, rec: dict) -> None:
        self.scans.append(scan_row(rec))

    def flush(self) -> None:
        if not (self.readings or self.scans):
            return
        conn = self._conn()
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                if self.readings:
                    batch = [self.readings[i] for i in range(min(len(self.readings), self.batch_max))]
                    with cur.copy(f"COPY env_readings ({', '.join(READING_COLS)}) FROM STDIN") as cp:
                        for row in batch:
                            cp.write_row(row)
                    for _ in batch:
                        self.readings.popleft()
                    self.sent_readings += len(batch)
                while self.scans:
                    row = self.scans[0]
                    cur.execute(
                        f"INSERT INTO scans ({', '.join(SCAN_COLS)}) VALUES ({', '.join(['%s'] * len(SCAN_COLS))}) "
                        "ON CONFLICT (scan_id) DO UPDATE SET health = EXCLUDED.health, status = EXCLUDED.status, "
                        "record = EXCLUDED.record", row)
                    self.scans.popleft()
                    self.sent_scans += 1
            self._backoff = 1.0
        except Exception:
            self._drop_conn()
            raise

    def close(self) -> None:
        self._drop_conn()

    def status(self) -> dict:
        return {"enabled": self.enabled, "connected": self.conn is not None,
                "pending_readings": len(self.readings), "pending_scans": len(self.scans),
                "sent_readings": self.sent_readings, "sent_scans": self.sent_scans}

    # ---- connection
    def _conn(self):
        if self.conn is not None:
            return self.conn
        if time.monotonic() < self._retry_at:
            return None
        try:
            if self._connect is not None:
                self.conn = self._connect(self.url)
            else:
                import psycopg
                self.conn = psycopg.connect(self.url, autocommit=True, connect_timeout=5,
                                            application_name=f"plantbot-{self.rover_id}")
            log.info("Tiger Data: connected")
            return self.conn
        except Exception:
            self._retry_at = time.monotonic() + self._backoff
            self._backoff = min(self._backoff * 2, 60.0)
            raise

    def _drop_conn(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None


# ---------------------------------------------------------------- CLI
def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m plantbot.integrations.tigerdata")
    ap.add_argument("--init-schema", action="store_true", help="apply sql/tigerdata_schema.sql")
    ap.add_argument("--demo-queries", action="store_true", help="run sql/tigerdata_queries.sql and print results")
    a = ap.parse_args()
    url = os.getenv("TIGERDATA_URL")
    if not url:
        raise SystemExit("set TIGERDATA_URL first")
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        if a.init_schema:
            for stmt in split_sql((SQL_DIR / "tigerdata_schema.sql").read_text()):
                print("--", stmt.splitlines()[0][:90])
                conn.execute(stmt)
            print("schema ok")
        if a.demo_queries:
            for stmt in split_sql((SQL_DIR / "tigerdata_queries.sql").read_text()):
                print("\n==", stmt.splitlines()[0][:90])
                cur = conn.execute(stmt)
                if cur.description:
                    print(" | ".join(d.name for d in cur.description))
                    for row in cur.fetchmany(20):
                        print(" | ".join(str(v) for v in row))


if __name__ == "__main__":
    _main()
