#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import os
import signal
import subprocess
import sys
import time
import csv
import base64
from pathlib import Path
from datetime import datetime, timezone

# ───────────────────────── CONFIG (constants only) ─────────────────────────
FAIRYRINGD = "fairyringd"
CHAIN_ID   = "fairyring_devnet"
HOME_DIR   = "/home/grider644/go/src/github.com/FairBlock/fairyring/devnet_data/fairyring_devnet"
KEYRING    = "test"
DENOM      = "ufairy"
GAS_PRICE  = "0.025"
NODE       = "tcp://127.0.0.1:26657"

SENDER_NAME    = "u1"
RECIPIENT_NAME = "u2"
FUNDER_NAME    = "wallet1"

SEND_AMOUNT_PER_MSG = 1
BASELINE_BLOCKS     = 10
CSV_PATH            = "msgsend_blocktime_results_step.csv"

# ── LINEAR growth: 1, 1000, 2000, 3000, ...
LINEAR_FIRST_SPECIAL = 1
LINEAR_START         = 200
LINEAR_STEP          = 200

# Gas heuristic for benchmark tx
BASE_GAS    = 120_000
GAS_PER_MSG = 45_000

# Funding behavior (fixed gas to avoid --gas auto lowball)
TOPUP_GAS         = 120_000
TOPUP_TIMEOUT_SEC = 120
TOPUP_MAX_RETRIES = 2

# Polling / timeouts
POLL_INTERVAL_SEC        = 0.50
TX_INCLUSION_TIMEOUT_SEC = 180

# Safety guard: typical Tendermint/CometBFT mempool max tx bytes (~1 MiB).
# Tune this if your node uses a different value.
MEMPOOL_MAX_TX_BYTES = 1_048_576

# Flags
TX_COMMON   = f"--home {HOME_DIR} --keyring-backend {KEYRING} --chain-id {CHAIN_ID}"
KEYS_COMMON = f"--home {HOME_DIR} --keyring-backend {KEYRING}"
Q_COMMON    = f"--node {NODE}"

# ────────────────────────── Utilities ──────────────────────────
def run(cmd: str) -> str:
    return subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT).decode()

def run_rc(cmd: str):
    p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.returncode, p.stdout.decode(errors="ignore"), p.stderr.decode(errors="ignore")

def jrun(cmd: str) -> dict:
    out = run(cmd + " --output json")
    first = out.find("{"); last = out.rfind("}")
    if first != -1 and last != -1 and last > first:
        try:
            return json.loads(out[first:last+1])
        except json.JSONDecodeError:
            pass
    preview = "\n".join(out.strip().splitlines()[:40])
    raise RuntimeError(f"Non-JSON response from:\n  {cmd}\n--- begin output ---\n{preview}\n--- end output ---")

def status() -> dict:
    out = run(f"{FAIRYRINGD} status {Q_COMMON}")
    return json.loads(out)

def parse_rfc3339nanos(ts: str) -> datetime:
    s = ts.strip()
    if s.endswith("Z"): s = s[:-1]
    if "." in s:
        base, frac = s.split(".", 1)
        frac = (frac + "000000")[:6]
        s = f"{base}.{frac}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def now_monotonic():
    return time.monotonic()

def keys_addr(name: str) -> str:
    try:
        out = run(f"{FAIRYRINGD} keys show {name} {KEYS_COMMON} --address")
        return out.strip()
    except subprocess.CalledProcessError:
        o = jrun(f"{FAIRYRINGD} keys show {name} {KEYS_COMMON}")
        return o["address"]

def ensure_key(name: str) -> str:
    try:
        return keys_addr(name)
    except subprocess.CalledProcessError:
        pass
    try:
        run(f"{FAIRYRINGD} keys add {name} {KEYS_COMMON} --algo secp256k1")
    except subprocess.CalledProcessError as e:
        print(f"keys add failed for {name}:\n{e.output.decode(errors='ignore')}", file=sys.stderr)
        raise
    return keys_addr(name)

def bank_balances(addr: str) -> dict:
    return jrun(f"{FAIRYRINGD} q bank balances {addr} {Q_COMMON}")

def coin_amount(bals: dict, denom: str) -> int:
    for c in bals.get("balances", []):
        if c.get("denom") == denom:
            return int(c.get("amount", '0'))
    return 0

def send_simple(from_name: str, to_addr: str, amount: int, denom: str, note: str = "", gas: int = TOPUP_GAS) -> dict:
    amt = f"{amount}{denom}"
    cmd = (
        f"{FAIRYRINGD} tx bank send {from_name} {to_addr} {amt} "
        f"{TX_COMMON} {Q_COMMON} --gas {gas} --gas-prices {GAS_PRICE}{denom} "
        f"--broadcast-mode=sync -y"
    )
    if note:
        cmd += f" --note '{note}'"
    return jrun(cmd)

def q_tx(txhash: str) -> dict:
    raw = jrun(f"{FAIRYRINGD} q tx {txhash} {Q_COMMON}")
    return raw.get("tx_response", raw)

def q_block(height: int) -> dict:
    return jrun(f"{FAIRYRINGD} q block --type=height {height} {Q_COMMON}")

def header_time_from_block(block_json: dict) -> datetime:
    t = block_json["header"]["time"]
    return parse_rfc3339nanos(t)

def latest_height_and_time() -> tuple[int, datetime]:
    st = status()
    sync = st.get("SyncInfo") or st.get("sync_info") or {}
    h = int(sync.get("latest_block_height") or 0)
    t = parse_rfc3339nanos(sync.get("latest_block_time"))
    return h, t

def wait_next_block(after_height: int | None = None) -> tuple[int, datetime]:
    if after_height is None:
        h0, _ = latest_height_and_time()
        after_height = h0
    while True:
        h, t = latest_height_and_time()
        if h > after_height:
            return h, t
        time.sleep(POLL_INTERVAL_SEC)

def block_time_for_height(height: int) -> float:
    if height <= 1:
        return float('nan')
    b  = q_block(height)
    bp = q_block(height - 1)
    t  = header_time_from_block(b)
    tp = header_time_from_block(bp)
    return (t - tp).total_seconds()

# ─────────────────── Account number / sequence helpers ────────────────────
def _parse_acct_nums(acc_json: dict) -> tuple[int, int]:
    acc = acc_json.get("account", acc_json)
    base = acc.get("base_account", acc)
    acct = int(base.get("account_number") or base.get("accountNumber") or 0)
    seq  = int(base.get("sequence") or base.get("Sequence") or 0)
    return acct, seq

def get_account_nums(addr: str) -> tuple[int, int]:
    data = jrun(f"{FAIRYRINGD} q auth account {addr} {Q_COMMON}")
    return _parse_acct_nums(data)

def wait_account_exists(addr: str, timeout_sec: int = 60) -> tuple[int, int]:
    start = time.monotonic()
    last_err = None
    while time.monotonic() - start < timeout_sec:
        try:
            return get_account_nums(addr)
        except Exception as e:
            last_err = e
            time.sleep(POLL_INTERVAL_SEC)
    raise RuntimeError(f"account {addr} still not found after {timeout_sec}s") from last_err

# ─────────────────────────── Funding / inclusion ──────────────────────────
def wait_balance_at_least(addr: str, denom: str, target: int, timeout_sec: int) -> int:
    start = time.monotonic()
    while True:
        try:
            bal = coin_amount(bank_balances(addr), denom)
            if bal >= target:
                return bal
        except Exception:
            pass
        if time.monotonic() - start > timeout_sec:
            raise TimeoutError(f"Timed out waiting balance >= {target} for {addr}")
        time.sleep(POLL_INTERVAL_SEC)

def ensure_sender_funded(funder_name: str, sender_addr: str, min_needed: int):
    before = coin_amount(bank_balances(sender_addr), DENOM)
    if before >= min_needed:
        print(f"[fund] sender already funded: {before} >= {min_needed} {DENOM}")
        return
    need  = min_needed - before
    gas   = TOPUP_GAS
    for attempt in range(1, TOPUP_MAX_RETRIES + 2):
        print(f"[fund] need {min_needed} {DENOM}, have {before}. topping up {need} {DENOM} from {FUNDER_NAME} (attempt {attempt}, gas={gas})")
        resp  = send_simple(funder_name, sender_addr, need, DENOM, note="topup", gas=gas)
        txh   = resp.get("txhash") or resp.get("txHash") or ""
        code  = int(resp.get("code", 0))
        if code != 0:
            raise RuntimeError(f"[fund] bank send broadcast failed code={code}: {resp.get('raw_log')}")
        print(f"[fund] broadcasted topup tx={txh or '<no-hash>'}. waiting for balance to reflect…")
        try:
            final_bal = wait_balance_at_least(sender_addr, DENOM, min_needed, timeout_sec=TOPUP_TIMEOUT_SEC)
            print(f"[fund] sender funded: {final_bal} {DENOM} (target {min_needed})")
            return
        except TimeoutError:
            gas = int(gas * 1.5)
            print(f"[fund] balance did not reach target in time; increasing gas to {gas} and retrying…")
    raise RuntimeError("[fund] unable to fund sender after retries (check node logs and tx fees/gas)")

def wait_for_inclusion(txhash: str, timeout_sec: int) -> dict:
    start = now_monotonic()
    while True:
        try:
            res = q_tx(txhash)
            height = int(res.get("height", 0))
            code = int(res.get("code", 0))
            if height > 0 or code != 0:
                return res
        except subprocess.CalledProcessError:
            pass
        if now_monotonic() - start > timeout_sec:
            raise TimeoutError(f"Timed out waiting for tx {txhash} inclusion")
        time.sleep(POLL_INTERVAL_SEC)

# ────────────────────── TX building (multi-message) ──────────────────────
def build_msgs_sends(n_msgs: int, from_addr: str, to_addr: str, denom: str, amount: int) -> list[dict]:
    coin = {"denom": denom, "amount": str(amount)}
    return [
        {
            "@type": "/cosmos.bank.v1beta1.MsgSend",
            "from_address": from_addr,
            "to_address": to_addr,
            "amount": [coin],
        }
        for _ in range(n_msgs)
    ]

def build_unsigned_tx(messages: list[dict], gas_limit: int, fee_amount: int, memo: str = "") -> dict:
    return {
        "body": {
            "messages": messages,
            "memo": memo,
            "timeout_height": "0",
            "extension_options": [],
            "non_critical_extension_options": []
        },
        "auth_info": {
            "signer_infos": [],
            "fee": {
                "amount": [{"denom": DENOM, "amount": str(fee_amount)}] if fee_amount > 0 else [],
                "gas_limit": str(gas_limit),
                "payer": "",
                "granter": ""
            }
        },
        "signatures": []
    }

# ────────────────────── Encode / size helpers ──────────────────────
def encoded_tx_bytes_len(signed_path: str) -> int:
    rc, out, err = run_rc(f"{FAIRYRINGD} tx encode {signed_path}")
    if rc != 0:
        rc2, out2, err2 = run_rc(f"{FAIRYRINGD} tx encode {signed_path} --output json")
        combined = out + "\n" + err + "\n" + out2 + "\n" + err2
        raise RuntimeError(f"[encode] failed to encode tx for size check (rc={rc}):\n{combined.strip()}")
    b64 = out.strip().strip('"')
    try:
        raw = base64.b64decode(b64, validate=True)
        return len(raw)
    except Exception as e:
        raise RuntimeError(f"[encode] could not decode base64 from tx encode:\n{out}\nerr:\n{err}") from e

def sign_tx_online(unsigned_path: str, signed_path: str, signer_name: str):
    print(f"[sign] signing tx: unsigned_file={unsigned_path}")
    rc, out, err = run_rc(
        f"{FAIRYRINGD} tx sign {unsigned_path} "
        f"--from {signer_name} {TX_COMMON} {Q_COMMON} "
        f"--output-document {signed_path}"
    )
    if rc != 0:
        print(f"[sign] ERROR rc={rc}\nstdout:\n{out}\nstderr:\n{err}", file=sys.stderr)
        raise subprocess.CalledProcessError(rc, "tx sign", output=(out+err).encode())
    size_fs = os.path.getsize(signed_path)
    try:
        enc_len = encoded_tx_bytes_len(signed_path)
    except Exception as e:
        enc_len = -1
        print(f"[sign] WARN: could not measure encoded size: {e}", file=sys.stderr)
    print(f"[sign] wrote signed_file={signed_path} fs_size={size_fs}B encoded_size={enc_len if enc_len>=0 else 'unknown'}B")

def broadcast_tx_verbose(signed_path: str) -> dict:
    try:
        enc_len = encoded_tx_bytes_len(signed_path)
        if enc_len > MEMPOOL_MAX_TX_BYTES:
            print(f"[broadcast] ABORT: encoded tx size {enc_len}B exceeds MEMPOOL_MAX_TX_BYTES={MEMPOOL_MAX_TX_BYTES}B")
            return {
                "code": 99,
                "raw_log": f"encoded tx size {enc_len}B exceeds configured MEMPOOL_MAX_TX_BYTES={MEMPOOL_MAX_TX_BYTES}B",
                "txhash": None,
                "height": 0,
            }
        else:
            print(f"[broadcast] encoded_size={enc_len}B <= limit {MEMPOOL_MAX_TX_BYTES}B — proceeding")
    except Exception as e:
        print(f"[broadcast] WARN: size pre-check failed: {e}", file=sys.stderr)

    cmd = f"{FAIRYRINGD} tx broadcast {signed_path} {Q_COMMON} --broadcast-mode=sync --output json"
    rc, out, err = run_rc(cmd)
    if rc == 0:
        try:
            return json.loads(out)
        except Exception:
            pass
    print(f"[broadcast] ERROR rc={rc}\nstdout (first 2000 chars):\n{out[:2000]}\n---\nstderr:\n{err}", file=sys.stderr)
    first = out.find("{"); last = out.rfind("}")
    if first != -1 and last != -1 and last > first:
        try:
            return json.loads(out[first:last+1])
        except Exception:
            pass
    return {"code": 98, "raw_log": f"broadcast failed rc={rc}; stdout/stderr attached above", "txhash": None, "height": 0}

def est_fee_amount(gas_limit: int) -> int:
    gp = float(GAS_PRICE)
    return math.ceil(gas_limit * gp)

# ──────────────────────── CSV handling & shutdown ────────────────────────
RESULTS: list[dict] = []
INTERRUPTED = False

def write_results_to_csv(path: str):
    if not RESULTS:
        return
    p = Path(path)
    write_header = not p.exists()
    with p.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "exp_index","n_msgs","txhash","height","code",
            "gas_used","gas_wanted","block_time_sec",
            "pr_block_time_sec","baseline_avg_sec","delta_sec",
            "pr_delta_sec","fee_amount","gas_limit","timestamp"
        ])
        if write_header:
            writer.writeheader()
        for row in RESULTS:
            writer.writerow(row)

def handle_sigint(sig, frame):
    global INTERRUPTED
    INTERRUPTED = True
    print("\nCtrl+C received — writing partial results to CSV…")
    try:
        write_results_to_csv(CSV_PATH)
        print(f"Saved partial results → {CSV_PATH}")
    except Exception as e:
        print("Failed to write CSV on interrupt:", e)
    finally:
        sys.exit(0)

signal.signal(signal.SIGINT, handle_sigint)

# ───────────────────────── linear message stream ─────────────────────────
def linear_msg_counts():
    """Yield 1, then 1000, 2000, 3000, ... forever."""
    yield LINEAR_FIRST_SPECIAL
    n = LINEAR_START
    while True:
        yield n
        n += LINEAR_STEP

# ─────────────────────────────── Main logic ───────────────────────────────
def main():
    # Ensure keys
    sender_addr = ensure_key(SENDER_NAME)
    recip_addr  = ensure_key(RECIPIENT_NAME)
    _           = ensure_key(FUNDER_NAME)

    funder_addr = keys_addr(FUNDER_NAME)
    funder_bal  = coin_amount(bank_balances(funder_addr), DENOM)
    print(f"Funder {FUNDER_NAME}={funder_addr} balance: {funder_bal} {DENOM}")
    if funder_bal <= 50_000_000:
        raise RuntimeError("Funder has insufficient balance in this keyring/home; top up the funder or switch FUNDER_NAME.")

    # Ensure sender funded
    ensure_sender_funded(FUNDER_NAME, sender_addr, min_needed=50_000_000)
    sb = coin_amount(bank_balances(sender_addr), DENOM)
    print(f"Sender {SENDER_NAME} balance after topup attempt: {sb} {DENOM}")

    # Account must exist now
    acct_num, seq = wait_account_exists(sender_addr, timeout_sec=60)
    print(f"Sender {SENDER_NAME}={sender_addr} on-chain: account_number={acct_num} sequence={seq}")

    # Baseline
    print(f"Measuring baseline over {BASELINE_BLOCKS} blocks…")
    h0, _t0 = latest_height_and_time()
    times = []
    prev_h, prev_t = wait_next_block(h0)
    for _ in range(BASELINE_BLOCKS):
        h, t = wait_next_block(prev_h)
        dt = (t - prev_t).total_seconds()
        times.append(dt)
        prev_h, prev_t = h, t
        print(f"  baseline block {h}: {dt:.3f}s")
    baseline_avg = sum(times) / len(times)
    print(f"Baseline average block time: {baseline_avg:.3f}s")

    # Benchmark loop (linear n_msgs)
    step_idx = 0
    last_seen_height, _ = latest_height_and_time()

    for n_msgs in linear_msg_counts():
        if INTERRUPTED:
            break

        msgs = build_msgs_sends(n_msgs, sender_addr, recip_addr, DENOM, SEND_AMOUNT_PER_MSG)

        gas_limit  = BASE_GAS + GAS_PER_MSG * n_msgs
        fee_amount = est_fee_amount(gas_limit)
        total_send = n_msgs * SEND_AMOUNT_PER_MSG

        print(f"[prep] step={step_idx} n_msgs={n_msgs} gas_limit={gas_limit} fee={fee_amount}")

        ensure_sender_funded(FUNDER_NAME, sender_addr, min_needed=total_send + fee_amount + 5_000_000)

        unsigned = build_unsigned_tx(messages=msgs, gas_limit=gas_limit, fee_amount=fee_amount, memo=f"bench step={step_idx} n={n_msgs}")
        unsigned_path = f"/tmp/unsigned_{int(time.time()*1000)}.json"
        signed_path   = f"/tmp/signed_{int(time.time()*1000)}.json"
        with open(unsigned_path, "w") as f:
            json.dump(unsigned, f)

        fs_size = os.path.getsize(unsigned_path)
        print(f"[prep] unsigned_file={unsigned_path} size={fs_size}B")

        # One-tx-per-block
        new_h, _ = wait_next_block(last_seen_height)
        last_seen_height = new_h

        try:
            sign_tx_online(unsigned_path, signed_path, SENDER_NAME)
            # Hard size guard before broadcast
            try:
                enc_len = encoded_tx_bytes_len(signed_path)
                if enc_len > MEMPOOL_MAX_TX_BYTES:
                    print(f"[guard] stopping: encoded tx size {enc_len}B > mempool limit {MEMPOOL_MAX_TX_BYTES}B")
                    break
            except Exception as e:
                print(f"[guard] WARN: could not pre-check tx size: {e}", file=sys.stderr)

            print(f"[step={step_idx} n={n_msgs}] broadcasting…")
            broadcast = broadcast_tx_verbose(signed_path)
        finally:
            try: os.remove(unsigned_path)
            except Exception: pass

        code  = int(broadcast.get("code", 0))
        txhash = broadcast.get("txhash") or broadcast.get("txHash") or ""
        if code != 0 or not txhash:
            print(f"[step={step_idx} n={n_msgs}] BROADCAST FAIL code={code} txhash={txhash} raw_log={broadcast.get('raw_log')}")
            break

        print(f"[step={step_idx} n={n_msgs}] broadcast hash={txhash} — waiting inclusion…")
        included = wait_for_inclusion(txhash, TX_INCLUSION_TIMEOUT_SEC)
        try: os.remove(signed_path)
        except Exception: pass

        inc_height = int(included.get("height", 0))
        inc_code   = int(included.get("code", 0))
        gas_used   = int(included.get("gas_used") or included.get("gasUsed") or 0)
        gas_wanted = int(included.get("gas_wanted") or included.get("gasWanted") or gas_limit)
        ts         = included.get("timestamp", "")

        if inc_code != 0:
            print(f"[step={step_idx} n={n_msgs}] Included with failure code={inc_code} at height={inc_height}. raw_log={included.get('raw_log')}")
            break

        blk_time = block_time_for_height(inc_height)
        delta    = blk_time - baseline_avg

        time.sleep(10)
        pr_blk_time = block_time_for_height(inc_height + 1)
        pr_delta    = pr_blk_time - baseline_avg

        row = {
            "exp_index": step_idx,
            "n_msgs": n_msgs,
            "txhash": txhash,
            "height": inc_height,
            "code": inc_code,
            "gas_used": gas_used,
            "gas_wanted": gas_wanted,
            "block_time_sec": f"{blk_time:.6f}",
            "pr_block_time_sec": f"{pr_blk_time:.6f}",
            "baseline_avg_sec": f"{baseline_avg:.6f}",
            "delta_sec": f"{delta:.6f}",
            "pr_delta_sec": f"{pr_delta:.6f}",
            "fee_amount": fee_amount,
            "gas_limit": gas_limit,
            "timestamp": ts,
        }
        RESULTS.append(row)

        print(f"[step={step_idx} n={n_msgs}] height={inc_height} gas_used={gas_used} block_time={blk_time:.3f}s (Δ={delta:+.3f}s)")

        step_idx += 1

    write_results_to_csv(CSV_PATH)
    print(f"\nWrote results → {CSV_PATH}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        handle_sigint(None, None)
