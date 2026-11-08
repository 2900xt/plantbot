"""Tamper-evident crop-health certificates on Solana DEVNET.

Per scan: sha256(raw.jpg) and sha256(canonical diagnosis JSON) go into a Memo-program
transaction signed by the rover's devnet keypair. Anyone can later recompute both hashes from
scans/<id>/ and compare them to the memo on-chain: if the photo or the verdict was edited after
the fact, the hashes no longer match.

DEVNET ONLY. The RPC's genesis hash is checked before the first transaction and the sink
refuses to sign for anything but devnet (or a local solana-test-validator on localhost).
Devnet SOL is free test money from the faucet; never put a mainnet keypair here.

Dependencies: `solders` (Rust wheel, abi3, has manylinux aarch64 builds, no other deps) for keys
and transaction encoding; JSON-RPC is plain urllib, so the heavier `solana` package isn't needed.

Env:  SOLANA_KEYPAIR   path to a solana-keygen style JSON keypair (keep it OUTSIDE the repo)
      SOLANA_RPC_URL   default https://api.devnet.solana.com

Output per scan: scans/<id>/solana.json {signature, explorer_url, image_sha256, diagnosis_sha256, ...}
(the dashboard can fetch it from /scans/<id>/solana.json) and sink.certs[scan_id].

CLI (devnet only):
  python -m plantbot.integrations.solana_log keygen  [--out ~/.config/plantbot/solana-devnet.json]
  python -m plantbot.integrations.solana_log airdrop [--sol 1]
  python -m plantbot.integrations.solana_log balance
  python -m plantbot.integrations.solana_log verify scans/<scan_id>
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from plantbot.integrations.sinks import Sink

log = logging.getLogger(__name__)

DEVNET_RPC = "https://api.devnet.solana.com"
DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"  # SPL Memo v2
MEMO_PREFIX = "plantbot-cert/v1 "
DEFAULT_KEYPAIR = "~/.config/plantbot/solana-devnet.json"

# Fields of the scan record that the certificate covers (all are in scans/<id>/scan.json).
DIAGNOSIS_KEYS = ("id", "t", "mode", "label", "p_healthy", "probs", "health", "status", "voc_stress",
                  "mq2_ratio", "gas_ratio", "diff_ratio", "temp_c", "rh", "backend", "explain")


# ---------------------------------------------------------------- hashing
def diagnosis_json(rec: dict) -> str:
    """Canonical JSON (sorted keys, no whitespace) of the diagnosis part of a scan record."""
    return json.dumps({k: rec.get(k) for k in DIAGNOSIS_KEYS}, sort_keys=True, separators=(",", ":"),
                      default=str, allow_nan=True)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def certificate(rec: dict, image: bytes, rover_id: str) -> dict:
    img, dx = sha256_hex(image), sha256_hex(diagnosis_json(rec).encode())
    memo = MEMO_PREFIX + json.dumps({"scan": rec["id"], "rover": rover_id, "img": img, "dx": dx,
                                     "status": rec.get("status"),
                                     "health": None if rec.get("health") is None else round(float(rec["health"]))},
                                    separators=(",", ":"))
    return {"scan_id": rec["id"], "rover_id": rover_id, "image_sha256": img, "diagnosis_sha256": dx,
            "memo": memo, "cluster": "devnet"}


def explorer_url(sig: str, rpc_url: str = DEVNET_RPC) -> str:
    if _is_local(rpc_url):
        return (f"https://explorer.solana.com/tx/{sig}?cluster=custom&customUrl="
                + urllib.parse.quote(rpc_url, safe=""))
    return f"https://explorer.solana.com/tx/{sig}?cluster=devnet"


def _is_local(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


# ---------------------------------------------------------------- rpc
class RPC:
    def __init__(self, url: str = DEVNET_RPC, timeout_s: float = 15, post=None):
        self.url = url
        self.timeout_s = timeout_s
        self._post = post  # tests inject: post(method, params) -> result
        self._checked = False

    def call(self, method: str, params: list | None = None):
        if self._post is not None:
            return self._post(method, params or [])
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json",
                                                                   "User-Agent": "plantbot/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            data = json.loads(r.read())
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error'].get('message', data['error'])}")
        return data["result"]

    def assert_devnet(self) -> None:
        """Hard stop for anything that isn't devnet (or a localhost test validator)."""
        if self._checked:
            return
        g = self.call("getGenesisHash")
        if g == MAINNET_GENESIS:
            raise RuntimeError("SOLANA_RPC_URL points at MAINNET: refusing (devnet only)")
        if g != DEVNET_GENESIS and not _is_local(self.url):
            raise RuntimeError(f"RPC genesis {g} is not devnet: refusing (devnet only)")
        self._checked = True


# ---------------------------------------------------------------- keys + tx
def load_keypair(path: str):
    from solders.keypair import Keypair

    with open(os.path.expanduser(path)) as f:
        return Keypair.from_bytes(bytes(json.load(f)))


def build_memo_tx(keypair, memo: str, blockhash: str) -> bytes:
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    ix = Instruction(Pubkey.from_string(MEMO_PROGRAM_ID), memo.encode(),
                     [AccountMeta(keypair.pubkey(), is_signer=True, is_writable=True)])
    bh = Hash.from_string(blockhash)
    msg = Message.new_with_blockhash([ix], keypair.pubkey(), bh)
    return bytes(Transaction([keypair], msg, bh))


def send_memo(rpc: RPC, keypair, memo: str, confirm_s: float = 20) -> tuple[str, str | None]:
    """Returns (signature, confirmation status or None if not confirmed within confirm_s)."""
    rpc.assert_devnet()
    bh = rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
    raw = build_memo_tx(keypair, memo, bh)
    sig = rpc.call("sendTransaction", [base64.b64encode(raw).decode(),
                                       {"encoding": "base64", "preflightCommitment": "confirmed"}])
    end = time.monotonic() + confirm_s
    while time.monotonic() < end:
        st = (rpc.call("getSignatureStatuses", [[sig]])["value"] or [None])[0]
        if st and st.get("err"):
            raise RuntimeError(f"tx {sig} failed: {st['err']}")
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig, st["confirmationStatus"]
        time.sleep(1.0)
    return sig, None


# ---------------------------------------------------------------- sink
class SolanaCertSink(Sink):
    name = "solana"
    wants_readings = False
    flush_s = 2.0
    queue_size = 100

    def __init__(self, cfg: dict | None = None, rover_id: str = "rover-1", store_root: str | None = None,
                 rpc: RPC | None = None, keypair=None):
        cfg = cfg or {}
        self.rover_id = rover_id
        self.store_root = store_root
        self.key_path = os.getenv("SOLANA_KEYPAIR") or ""
        self.rpc = rpc or RPC(os.getenv("SOLANA_RPC_URL") or DEVNET_RPC, float(cfg.get("timeout_s", 15)))
        self.confirm_s = float(cfg.get("confirm_s", 20))
        self.keypair = keypair
        self.pending: list[dict] = []
        self.certs: dict[str, dict] = {}
        self.enabled = bool(keypair is not None or self.key_path) and cfg.get("enabled", True)
        if self.enabled and keypair is None:
            try:
                import solders  # noqa: F401
            except ImportError:
                log.warning("Solana: SOLANA_KEYPAIR set but solders is not installed (pip install solders)")
                self.enabled = False
        if not self.enabled:
            log.info("Solana certificates disabled (set SOLANA_KEYPAIR to a devnet keypair to enable)")

    def start(self) -> None:
        if self.keypair is None:
            self.keypair = load_keypair(self.key_path)
        log.info("Solana devnet certs from %s via %s", self.keypair.pubkey(), self.rpc.url)

    def on_scan(self, rec: dict) -> None:
        if len(self.pending) >= 50:
            self.pending.pop(0)
        self.pending.append(rec)
        self.flush()  # one tx per scan; send right away (flush() also retries leftovers)

    def flush(self) -> None:
        if self.pending and self.keypair is None:
            self.start()  # keypair file missing at boot: keep retrying instead of crashing
        while self.pending:
            rec = self.pending[0]
            path = rec.get("image_path") or (self.store_root and os.path.join(self.store_root, rec["id"], "raw.jpg"))
            try:
                with open(path, "rb") as f:
                    image = f.read()
            except (OSError, TypeError) as e:
                log.error("Solana: no image for scan %s (%s); skipping", rec.get("id"), e)
                self.pending.pop(0)
                continue
            cert = certificate(rec, image, rec.get("rover_id") or self.rover_id)
            sig, conf = send_memo(self.rpc, self.keypair, cert["memo"], self.confirm_s)  # raises -> retried
            cert.update(signature=sig, confirmation=conf, explorer_url=explorer_url(sig, self.rpc.url),
                        signer=str(self.keypair.pubkey()), memo_program=MEMO_PROGRAM_ID, sent_at=time.time())
            self.certs[rec["id"]] = cert
            self._write(path, cert)
            self.pending.pop(0)
            log.info("Solana cert for %s: %s", rec["id"], cert["explorer_url"])

    @staticmethod
    def _write(image_path: str, cert: dict) -> None:
        try:
            out = Path(image_path).with_name("solana.json")
            out.write_text(json.dumps(cert, indent=2))
        except OSError as e:
            log.warning("Solana: couldn't write solana.json: %s", e)

    def cert(self, scan_id: str) -> dict | None:
        return self.certs.get(scan_id)

    def status(self) -> dict:
        last = next(reversed(self.certs.values()), None) if self.certs else None
        return {"enabled": self.enabled, "cluster": "devnet", "pending": len(self.pending),
                "certified": len(self.certs), "last": last and {k: last[k] for k in ("scan_id", "explorer_url")},
                "signer": str(self.keypair.pubkey()) if self.keypair is not None else None}


# ---------------------------------------------------------------- verify + CLI
def verify(scan_dir: str, rpc: RPC | None = None) -> dict:
    """Recompute hashes from scan_dir and compare with solana.json and (if rpc) the on-chain memo."""
    d = Path(scan_dir)
    rec = json.loads((d / "scan.json").read_text())
    cert = json.loads((d / "solana.json").read_text())
    img = sha256_hex((d / "raw.jpg").read_bytes())
    dx = sha256_hex(diagnosis_json(rec).encode())
    out = {"image_ok": img == cert["image_sha256"], "diagnosis_ok": dx == cert["diagnosis_sha256"],
           "signature": cert["signature"], "explorer_url": cert.get("explorer_url")}
    if rpc is not None:
        tx = rpc.call("getTransaction", [cert["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                             "maxSupportedTransactionVersion": 0}])
        memos = [ix.get("parsed") for ix in (tx or {}).get("transaction", {}).get("message", {}).get("instructions", [])
                 if ix.get("programId") == MEMO_PROGRAM_ID]
        onchain = next((m for m in memos if isinstance(m, str) and m.startswith(MEMO_PREFIX)), None)
        out["onchain_found"] = onchain is not None
        if onchain:
            body = json.loads(onchain[len(MEMO_PREFIX):])
            out["onchain_ok"] = body.get("img") == img and body.get("dx") == dx
    return out


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m plantbot.integrations.solana_log",
                                 description="Solana DEVNET helper for plantbot crop-health certificates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="make a new devnet keypair file (chmod 600)")
    k.add_argument("--out", default=os.getenv("SOLANA_KEYPAIR") or DEFAULT_KEYPAIR)
    a = sub.add_parser("airdrop", help="request free devnet SOL")
    a.add_argument("--sol", type=float, default=1.0)
    sub.add_parser("balance")
    v = sub.add_parser("verify", help="recompute a scan's hashes and check them against the chain")
    v.add_argument("scan_dir")
    args = ap.parse_args()
    rpc = RPC(os.getenv("SOLANA_RPC_URL") or DEVNET_RPC)

    if args.cmd == "keygen":
        from solders.keypair import Keypair

        out = Path(os.path.expanduser(args.out))
        if out.exists():
            raise SystemExit(f"{out} exists; not overwriting")
        out.parent.mkdir(parents=True, exist_ok=True)
        kp = Keypair()
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(list(bytes(kp)), f)
        print(f"wrote {out}\npubkey {kp.pubkey()}\nnext: SOLANA_KEYPAIR={out} python -m plantbot.integrations.solana_log airdrop")
        return

    rpc.assert_devnet()  # everything below talks to the network: devnet only
    kp = load_keypair(os.getenv("SOLANA_KEYPAIR") or DEFAULT_KEYPAIR) if args.cmd != "verify" else None
    if args.cmd == "airdrop":
        lamports = int(min(args.sol, 2.0) * 1e9)  # devnet faucet caps per request
        try:
            sig = rpc.call("requestAirdrop", [str(kp.pubkey()), lamports])
            print(f"airdrop requested: {explorer_url(sig, rpc.url)}")
        except Exception as e:
            print(f"airdrop failed ({e}).\nThe public devnet faucet is rate-limited: use https://faucet.solana.com "
                  f"with address {kp.pubkey()} (sign in with GitHub for a higher limit).")
    elif args.cmd == "balance":
        lam = rpc.call("getBalance", [str(kp.pubkey())])["value"]
        print(f"{kp.pubkey()}: {lam / 1e9:.4f} devnet SOL (~{lam // 5000} memo txs)")
    elif args.cmd == "verify":
        print(json.dumps(verify(args.scan_dir, rpc), indent=2))


if __name__ == "__main__":
    _main()
