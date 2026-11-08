"""Snowflake sink: scan records -> PLANTBOT.FIELD.SCANS via the Snowflake SQL REST API.

Why the REST API and not snowflake-connector-python: the connector does ship aarch64 cp313
wheels, but it drags in cryptography, pyOpenSSL, boto3/botocore and friends (~60 MB on the Pi).
The SQL API is one HTTPS POST with a programmatic access token (PAT), so this file uses only
the standard library.

Scans are buffered and MERGEd every `flush_s` (default 60 s) as one JSON array bound to a single
parameter, so a batch is one statement and re-sends are idempotent (MERGE on SCAN_ID).

Env (all required to enable):
  SNOWFLAKE_ACCOUNT    account identifier, e.g. myorg-myaccount  (Admin -> Accounts -> locator URL)
  SNOWFLAKE_PAT        programmatic access token secret
Optional: SNOWFLAKE_WAREHOUSE (PLANTBOT_WH), SNOWFLAKE_DATABASE (PLANTBOT), SNOWFLAKE_SCHEMA (FIELD),
          SNOWFLAKE_ROLE, SNOWFLAKE_HOST (override hostname), SNOWFLAKE_CORTEX_MODEL (llama3.1-70b)

CLI:
  python -m plantbot.integrations.snowflake_sink --init-schema    # sql/snowflake_schema.sql
  python -m plantbot.integrations.snowflake_sink --report         # Cortex AI field report
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from plantbot.integrations.sinks import Sink

log = logging.getLogger(__name__)
SQL_DIR = Path(__file__).resolve().parent / "sql"

MERGE_SQL = """
MERGE INTO SCANS t
USING (SELECT f.value AS v FROM TABLE(FLATTEN(INPUT => PARSE_JSON(?))) f) s
ON t.SCAN_ID = s.v:id::STRING
WHEN MATCHED THEN UPDATE SET HEALTH = s.v:health::FLOAT, STATUS = s.v:status::STRING, RECORD = s.v
WHEN NOT MATCHED THEN INSERT
  (SCAN_ID, TS, ROVER_ID, PLANT_ID, MODE, LABEL, P_HEALTHY, HEALTH, STATUS, VOC_STRESS,
   MQ2_RATIO, GAS_RATIO, DIFF_RATIO, TEMP_C, RH, BACKEND, EXPLAIN, RECORD)
VALUES
  (s.v:id::STRING, s.v:ts::TIMESTAMP_TZ, s.v:rover_id::STRING, s.v:plant_id::STRING, s.v:mode::STRING,
   s.v:label::STRING, s.v:p_healthy::FLOAT, s.v:health::FLOAT, s.v:status::STRING, s.v:voc_stress::FLOAT,
   s.v:mq2_ratio::FLOAT, s.v:gas_ratio::FLOAT, s.v:diff_ratio::FLOAT, s.v:temp_c::FLOAT, s.v:rh::FLOAT,
   s.v:backend::STRING, s.v:explain::STRING, s.v)
"""

REPORT_SQL = """
WITH recent AS (
  SELECT ROVER_ID, COALESCE(PLANT_ID, SCAN_ID) AS PLANT, TS, LABEL, STATUS, HEALTH, VOC_STRESS
  FROM SCANS WHERE TS > DATEADD(hour, -24, CURRENT_TIMESTAMP())
  ORDER BY TS DESC LIMIT 60
)
SELECT AI_COMPLETE(?, 'You are an agronomist reviewing a field robot''s plant scans from the last 24 h. '
  || 'Each line: time, plant, leaf-model label, status, fused health 0-100, VOC stress 0-1. '
  || 'Write 4 short bullet points: overall field health, which plants to check first and why, '
  || 'any VOC-vs-image disagreement worth a second look, and one concrete next action.\\n'
  || LISTAGG(TO_VARCHAR(TS, 'HH24:MI') || ' ' || PLANT || ' ' || LABEL || ' ' || STATUS || ' '
             || ROUND(HEALTH, 0) || ' ' || ROUND(COALESCE(VOC_STRESS, 0), 2), '\\n')
     WITHIN GROUP (ORDER BY TS)) AS REPORT,
  COUNT(*) AS N
FROM recent
"""


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(float(t), dt.timezone.utc).isoformat(timespec="milliseconds")


def scan_doc(rec: dict) -> dict:
    """The JSON object that lands in SCANS.RECORD (VARIANT); typed columns are pulled out in SQL."""
    d = {k: v for k, v in rec.items() if k != "image_path"}
    d["ts"] = _iso(rec.get("t") or time.time())
    return d


class SnowflakeError(RuntimeError):
    pass


class SnowflakeSQLAPI:
    """Minimal client for POST /api/v2/statements (sync with async-poll fallback)."""

    def __init__(self, account: str, token: str, warehouse: str | None = None, database: str | None = None,
                 schema: str | None = None, role: str | None = None, host: str | None = None,
                 timeout_s: float = 30, opener=None):
        host = host or f"{account.lower().replace('_', '-')}.snowflakecomputing.com"
        self.base = f"https://{host}/api/v2/statements"
        self.token = token
        self.ctx = {k: v for k, v in (("warehouse", warehouse), ("database", database),
                                       ("schema", schema), ("role", role)) if v}
        self.timeout_s = timeout_s
        self._open = opener or urllib.request.urlopen  # tests inject a fake

    def _req(self, url: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST", headers={
            "Authorization": f"Bearer {self.token}",
            "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "plantbot/1.0"})
        try:
            with self._open(req, timeout=self.timeout_s + 10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()[:500].decode(errors="replace")
            raise SnowflakeError(f"HTTP {e.code}: {raw}") from None

    def execute(self, statement: str, params: list | None = None) -> list[list]:
        body = {"statement": statement, "timeout": int(self.timeout_s), **self.ctx}
        if params:
            body["bindings"] = {str(i + 1): {"type": "TEXT" if isinstance(p, str) else "REAL"
                                             if isinstance(p, float) else "FIXED", "value": str(p)}
                                for i, p in enumerate(params)}
        status, data = self._req(self.base, body)
        deadline = time.monotonic() + self.timeout_s
        while status == 202 and time.monotonic() < deadline:  # still running: poll the handle
            time.sleep(0.5)
            status, data = self._req(f"{self.base}/{data['statementHandle']}")
        if status != 200:
            raise SnowflakeError(f"statement not finished (HTTP {status}): {data.get('message')}")
        return data.get("data") or []


def client_from_env(**kw) -> SnowflakeSQLAPI | None:
    acct, pat = os.getenv("SNOWFLAKE_ACCOUNT"), os.getenv("SNOWFLAKE_PAT")
    if not (acct and pat):
        return None
    return SnowflakeSQLAPI(acct, pat,
                           warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "PLANTBOT_WH"),
                           database=os.getenv("SNOWFLAKE_DATABASE", "PLANTBOT"),
                           schema=os.getenv("SNOWFLAKE_SCHEMA", "FIELD"),
                           role=os.getenv("SNOWFLAKE_ROLE"), host=os.getenv("SNOWFLAKE_HOST"), **kw)


class SnowflakeSink(Sink):
    name = "snowflake"
    wants_readings = False   # Tiger Data owns the 10 Hz stream; Snowflake gets the scans
    queue_size = 500

    def __init__(self, cfg: dict | None = None, rover_id: str = "rover-1", client: SnowflakeSQLAPI | None = None):
        cfg = cfg or {}
        self.rover_id = rover_id
        self.flush_s = float(cfg.get("flush_s", 60))
        self.client = client or client_from_env(timeout_s=float(cfg.get("timeout_s", 30)))
        self.pending: collections.deque = collections.deque(maxlen=int(cfg.get("buffer_max", 500)))
        self.sent = 0
        self.last_report: str | None = None
        self.enabled = self.client is not None and cfg.get("enabled", True)
        if not self.enabled:
            log.info("Snowflake sink disabled (set SNOWFLAKE_ACCOUNT and SNOWFLAKE_PAT to enable)")

    def on_scan(self, rec: dict) -> None:
        self.pending.append(scan_doc(rec))

    def flush(self) -> None:
        if not self.pending:
            return
        batch = list(self.pending)
        self.client.execute(MERGE_SQL, [json.dumps(batch, default=str)])
        for _ in batch:
            self.pending.popleft()
        self.sent += len(batch)

    def field_report(self, model: str | None = None) -> str | None:
        """Cortex AI summary of the last 24 h of scans (blocking; call from a worker thread)."""
        model = model or os.getenv("SNOWFLAKE_CORTEX_MODEL", "llama3.1-70b")
        rows = self.client.execute(REPORT_SQL, [model])
        self.last_report = rows[0][0] if rows and rows[0] else None
        return self.last_report

    def status(self) -> dict:
        return {"enabled": self.enabled, "pending_scans": len(self.pending), "sent_scans": self.sent}


# ---------------------------------------------------------------- CLI
def _main() -> None:
    import argparse

    from plantbot.integrations.tigerdata import split_sql

    ap = argparse.ArgumentParser(prog="python -m plantbot.integrations.snowflake_sink")
    ap.add_argument("--init-schema", action="store_true")
    ap.add_argument("--report", action="store_true", help="print a Cortex AI field report")
    ap.add_argument("--model")
    a = ap.parse_args()
    c = client_from_env()
    if c is None:
        raise SystemExit("set SNOWFLAKE_ACCOUNT and SNOWFLAKE_PAT first")
    if a.init_schema:
        # Bootstrap statements create the DB/schema/warehouse, so run them without that context.
        boot = SnowflakeSQLAPI.__new__(SnowflakeSQLAPI)
        boot.__dict__.update(c.__dict__, ctx={k: v for k, v in c.ctx.items() if k == "role"})
        for stmt in split_sql((SQL_DIR / "snowflake_schema.sql").read_text()):
            print("--", stmt.splitlines()[0][:90])
            boot.execute(stmt)
        print("schema ok")
    if a.report:
        print(SnowflakeSink({}, client=c).field_report(a.model))


if __name__ == "__main__":
    _main()
