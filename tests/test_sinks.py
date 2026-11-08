"""Telemetry sinks: fan-out behaviour + each sink against fakes. No network, no cloud accounts."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from plantbot.integrations import sinks as S
from plantbot.integrations.sinks import FanOut, Sink, build_fanout


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Recorder(Sink):
    name = "rec"
    enabled = True
    flush_s = 0.05

    def __init__(self, slow: float = 0.0, fail: bool = False):
        self.readings, self.scans, self.flushes = [], [], 0
        self.slow, self.fail = slow, fail

    def on_reading(self, s):
        if self.slow:
            time.sleep(self.slow)
        self.readings.append(s)

    def on_scan(self, rec):
        if self.fail:
            raise RuntimeError("boom")
        self.scans.append(rec)

    def flush(self):
        self.flushes += 1


SCAN = {"id": "20261108-010203-abcd", "t": 1794100000.25, "mode": "manual", "label": "Tomato___Early_blight",
        "p_healthy": 0.21, "probs": {"Tomato___Early_blight": 0.79}, "health": 33.5, "status": "stressed",
        "voc_stress": 0.4, "mq2_ratio": 1.2, "gas_ratio": 1.1, "diff_ratio": 1.05, "temp_c": 23.1, "rh": 41.0,
        "backend": "dummy", "explain": "img 0.21, voc 0.4", "infer_ms": 12.0}


# ---------------------------------------------------------------- env: nothing configured
@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("TIGERDATA_URL", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_PAT", "SOLANA_KEYPAIR", "SOLANA_RPC_URL",
              "PLANTBOT_ROVER_ID"):
        monkeypatch.delenv(k, raising=False)


def test_build_fanout_without_env_is_a_noop():
    fan = build_fanout(None)
    assert not fan.enabled and fan.runners == []
    fan.start()
    fan.on_reading({"t": 1.0, "mq2_v": 0.5})
    fan.on_scan(SCAN)
    st = fan.status()
    assert set(st) == {"tigerdata", "snowflake", "solana"}
    assert all(v == {"enabled": False} for v in st.values())
    fan.close()


def test_build_fanout_respects_config_disable(monkeypatch):
    monkeypatch.setenv("TIGERDATA_URL", "postgres://x")
    cfg = {"integrations": {"tigerdata": {"enabled": False}}, "leaflink": {"device": "rover-7"}}
    fan = build_fanout(cfg)
    assert "tigerdata" not in fan.status() or not fan.status()["tigerdata"]["enabled"]
    assert fan.rover_id == "rover-7"


# ---------------------------------------------------------------- fan-out
def test_fanout_delivers_and_flushes():
    r = Recorder()
    fan = FanOut([r], "rover-1").start()
    fan.on_reading({"t": 1.0, "mq2_v": 0.5, "voc_top": float("nan")})
    fan.on_scan(SCAN, image_path="/tmp/x.jpg")
    assert wait_for(lambda: r.readings and r.scans and r.flushes)
    assert r.readings[0]["rover_id"] == "rover-1" and r.readings[0]["voc_top"] is None  # NaN cleaned
    assert r.scans[0]["image_path"] == "/tmp/x.jpg" and r.scans[0]["rover_id"] == "rover-1"
    fan.close()


def test_slow_sink_never_blocks_caller_and_drops_when_full():
    slow = Recorder(slow=0.2)
    slow.queue_size = 3
    fast = Recorder()
    fan = FanOut([slow, fast], "rover-1").start()
    t0 = time.perf_counter()
    for i in range(50):
        fan.on_reading({"t": float(i)})
    assert time.perf_counter() - t0 < 0.1  # never waits on the slow sink
    assert wait_for(lambda: len(fast.readings) == 50)
    run = next(r for r in fan.runners if r.sink is slow)
    assert run.dropped > 0
    fan.close()


def test_failing_sink_is_isolated():
    bad, good = Recorder(fail=True), Recorder()
    bad.name = "bad"
    fan = FanOut([bad, good], "rover-1").start()
    fan.on_scan(SCAN)
    fan.on_scan(SCAN)
    assert wait_for(lambda: len(good.scans) == 2)
    assert wait_for(lambda: fan.status()["bad"]["errors"] == 2)
    assert "boom" in fan.status()["bad"]["error"]
    fan.close()


def test_sampler_poller_forwards_new_readings_only():
    class FakeSampler:
        warm, _sniffing = True, 0
        cfg = SimpleNamespace(sample_hz=100)

        def __init__(self):
            self.r = SimpleNamespace(t=1.0, mq2_v=0.4, gas_ohms=5e4, temp_c=22.0, rh=40.0, pressure_hpa=1010.0)

        def latest(self):
            return self.r

        def voc_pair(self):
            return 0.12, 0.03

    sm, rec = FakeSampler(), Recorder()
    fan = FanOut([rec], "rover-1").attach_sampler(sm, hz=100).start()
    assert wait_for(lambda: len(rec.readings) == 1)
    time.sleep(0.1)
    assert len(rec.readings) == 1  # same reading not re-sent
    sm.r = SimpleNamespace(**{**vars(sm.r), "t": 2.0})
    assert wait_for(lambda: len(rec.readings) == 2)
    assert rec.readings[1]["voc_top"] == 0.12 and rec.readings[1]["warm"] is True
    fan.close()


# ---------------------------------------------------------------- Tiger Data
class FakeCopy:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write_row(self, row):
        self.sink.append(row)


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def copy(self, sql):
        assert sql.startswith("COPY env_readings")
        if self.db.down:
            raise ConnectionError("offline")
        return FakeCopy(self.db.rows)

    def execute(self, sql, params):
        assert "INSERT INTO scans" in sql and "ON CONFLICT (scan_id)" in sql
        self.db.scans.append(params)


class FakeDB:
    def __init__(self):
        self.rows, self.scans, self.down, self.connects = [], [], False, 0

    def connect(self, url):
        self.connects += 1
        return self

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


def test_tigerdata_disabled_without_url():
    from plantbot.integrations.tigerdata import TigerDataSink

    assert not TigerDataSink({}).enabled


def test_tigerdata_batches_and_survives_outage(monkeypatch):
    from plantbot.integrations.tigerdata import TigerDataSink

    monkeypatch.setenv("TIGERDATA_URL", "postgres://fake")
    db = FakeDB()
    s = TigerDataSink({"buffer_max": 5}, rover_id="rover-1", connect=db.connect)
    assert s.enabled
    for i in range(8):  # more than buffer_max: oldest dropped
        s.on_reading(S.reading_dict({"t": 1794100000 + i, "voc_top": 0.1}, "rover-1"))
    s.on_scan({**SCAN, "rover_id": "rover-1", "image_path": "/x"})
    db.down = True
    with pytest.raises(ConnectionError):
        s.flush()
    assert len(s.readings) == 5 and s.conn is None  # kept, connection dropped for reconnect
    db.down = False
    s._retry_at = 0
    s.flush()
    assert len(db.rows) == 5 and db.rows[0][0].year == 2026 and db.rows[0][1] == "rover-1"
    assert len(db.scans) == 1 and db.scans[0][0] == SCAN["id"]
    assert "image_path" not in json.loads(db.scans[0][-1])
    assert s.status()["sent_readings"] == 5


def test_split_sql_handles_dollar_quotes_and_comments():
    from plantbot.integrations.tigerdata import SQL_DIR, split_sql

    parts = split_sql("SELECT 'a;b'; -- c;d\nDO $$ BEGIN PERFORM 1; END $$; SELECT 2")
    assert parts == ["SELECT 'a;b'", "DO $$ BEGIN PERFORM 1; END $$", "SELECT 2"]
    schema = split_sql((SQL_DIR / "tigerdata_schema.sql").read_text())
    assert any("tsdb.hypertable" in p for p in schema)
    assert any("timescaledb.continuous" in p for p in schema)
    assert any("add_retention_policy" in p for p in schema)


# ---------------------------------------------------------------- Snowflake
class FakeResp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def test_snowflake_disabled_without_env():
    from plantbot.integrations.snowflake_sink import SnowflakeSink

    assert not SnowflakeSink({}).enabled


def test_snowflake_merge_batch_and_async_poll(monkeypatch):
    from plantbot.integrations import snowflake_sink as SF

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "myorg_myacct")
    monkeypatch.setenv("SNOWFLAKE_PAT", "secret-pat")
    monkeypatch.setattr(SF.time, "sleep", lambda s: None)
    calls = []

    def opener(req, timeout):
        calls.append(req)
        if req.get_method() == "POST":
            return FakeResp(202, {"statementHandle": "h-1"})
        return FakeResp(200, {"data": [["1"]]})

    sink = SF.SnowflakeSink({}, client=SF.client_from_env(opener=opener))
    assert sink.enabled and not sink.wants_readings
    sink.on_scan({**SCAN, "rover_id": "rover-1", "image_path": "/x"})
    sink.on_scan({**SCAN, "id": "second", "rover_id": "rover-1"})
    sink.flush()
    post = calls[0]
    assert post.full_url == "https://myorg-myacct.snowflakecomputing.com/api/v2/statements"
    assert post.get_header("Authorization") == "Bearer secret-pat"
    assert post.get_header("X-snowflake-authorization-token-type") == "PROGRAMMATIC_ACCESS_TOKEN"
    body = json.loads(post.data)
    assert body["statement"].strip().startswith("MERGE INTO SCANS")
    assert body["warehouse"] == "PLANTBOT_WH" and body["database"] == "PLANTBOT"
    docs = json.loads(body["bindings"]["1"]["value"])
    assert [d["id"] for d in docs] == [SCAN["id"], "second"] and "image_path" not in docs[0]
    assert docs[0]["ts"].startswith("2026-")
    assert calls[1].full_url.endswith("/statements/h-1")  # polled the async handle
    assert sink.status() == {"enabled": True, "pending_scans": 0, "sent_scans": 2}


def test_snowflake_keeps_batch_on_http_error(monkeypatch):
    import io
    import urllib.error

    from plantbot.integrations import snowflake_sink as SF

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"message":"bad token"}'))

    sink = SF.SnowflakeSink({}, client=SF.SnowflakeSQLAPI("acct", "pat", opener=opener))
    sink.on_scan(SCAN)
    with pytest.raises(SF.SnowflakeError, match="401"):
        sink.flush()
    assert len(sink.pending) == 1


# ---------------------------------------------------------------- Solana
def test_certificate_is_deterministic_and_tamper_evident():
    from plantbot.integrations.solana_log import MEMO_PREFIX, certificate

    a = certificate(SCAN, b"jpeg-bytes", "rover-1")
    b = certificate(json.loads(json.dumps(SCAN)), b"jpeg-bytes", "rover-1")  # round-trip like scan.json
    assert a == b and a["memo"].startswith(MEMO_PREFIX) and len(a["memo"].encode()) < 400
    assert certificate({**SCAN, "health": 90.0}, b"jpeg-bytes", "rover-1")["diagnosis_sha256"] != a["diagnosis_sha256"]
    assert certificate(SCAN, b"jpeg-bytez", "rover-1")["image_sha256"] != a["image_sha256"]


def test_solana_disabled_without_keypair():
    from plantbot.integrations.solana_log import SolanaCertSink

    assert not SolanaCertSink({}).enabled


class FakeChain:
    def __init__(self, genesis):
        self.genesis, self.sent = genesis, []

    def __call__(self, method, params):
        if method == "getGenesisHash":
            return self.genesis
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": "11111111111111111111111111111111"}}
        if method == "sendTransaction":
            self.sent.append(params[0])
            return "5" * 87
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "confirmed", "err": None}]}
        raise AssertionError(method)


def test_solana_sink_writes_cert_on_devnet(tmp_path):
    solders = pytest.importorskip("solders")  # noqa: F841
    import base64

    from solders.keypair import Keypair
    from solders.transaction import Transaction

    from plantbot.integrations import solana_log as SL

    d = tmp_path / SCAN["id"]
    d.mkdir()
    (d / "raw.jpg").write_bytes(b"\xff\xd8fake")
    (d / "scan.json").write_text(json.dumps(SCAN))
    chain = FakeChain(SL.DEVNET_GENESIS)
    kp = Keypair()
    sink = SL.SolanaCertSink({"confirm_s": 1}, rover_id="rover-1", store_root=str(tmp_path),
                             rpc=SL.RPC(SL.DEVNET_RPC, post=chain), keypair=kp)
    assert sink.enabled
    sink.on_scan({**SCAN, "rover_id": "rover-1"})
    cert = json.loads((d / "solana.json").read_text())
    assert cert["explorer_url"].endswith("?cluster=devnet") and cert["confirmation"] == "confirmed"
    tx = Transaction.from_bytes(base64.b64decode(chain.sent[0]))
    assert tx.verify() is None  # validly signed
    memo = bytes(tx.message.instructions[0].data).decode()
    assert cert["image_sha256"] in memo and cert["diagnosis_sha256"] in memo
    v = SL.verify(str(d))
    assert v["image_ok"] and v["diagnosis_ok"]
    (d / "raw.jpg").write_bytes(b"\xff\xd8edited")
    assert not SL.verify(str(d))["image_ok"]


def test_solana_refuses_mainnet():
    from plantbot.integrations import solana_log as SL

    rpc = SL.RPC("https://api.mainnet-beta.solana.com", post=FakeChain(SL.MAINNET_GENESIS))
    with pytest.raises(RuntimeError, match="MAINNET"):
        SL.send_memo(rpc, keypair=None, memo="x")
    rpc = SL.RPC("https://rpc.example.com", post=FakeChain("SomeOtherGenesis1111111111111111111111111111"))
    with pytest.raises(RuntimeError, match="not devnet"):
        rpc.assert_devnet()
    # local solana-test-validator is fine
    SL.RPC("http://127.0.0.1:8899", post=FakeChain("LocalGenesis")).assert_devnet()


def test_solana_failure_keeps_scan_pending(tmp_path):
    pytest.importorskip("solders")
    from solders.keypair import Keypair

    from plantbot.integrations import solana_log as SL

    d = tmp_path / SCAN["id"]
    d.mkdir()
    (d / "raw.jpg").write_bytes(b"x")

    def offline(method, params):
        raise OSError("network unreachable")

    sink = SL.SolanaCertSink({}, store_root=str(tmp_path), rpc=SL.RPC(post=offline), keypair=Keypair())
    with pytest.raises(OSError):
        sink.on_scan(SCAN)
    assert len(sink.pending) == 1 and not (d / "solana.json").exists()
