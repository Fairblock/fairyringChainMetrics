#!/usr/bin/env python3
import csv
import os
import re
import subprocess
import sys
import time
import tempfile
from pathlib import Path

# ───────────────────────── CONFIG ─────────────────────────
FAIRYRINGD = "fairyringd"
CHAIN_ID = "fairyring_devnet"
HOME_DIR = "./devnet_data/fairyring_devnet"
KEYRING_BACKEND = "test"
FROM = "fairy1m9l358xunhhwds0568za49mzhvuxx9uxdra8sq"
TARGET_BLOCK_HEIGHT = "10000"

ROUNDS = 25                # 1, 2, 4, ..., 2^24 chars
CSV_PATH = "gas_benchmark_submit_encrypted_tx.csv"
SLEEP_BETWEEN_RUNS_SEC = 0.3
SUBPROCESS_TIMEOUT_SEC = 120

# ───────────────────────── HELPERS ─────────────────────────
GAS_RE = re.compile(r"gas estimate:\s*(\d+)", re.IGNORECASE)

def run_cmd(args):
    try:
        p = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"command timed out after {SUBPROCESS_TIMEOUT_SEC}s")
    except OSError as e:
        raise RuntimeError(f"OSError while invoking CLI: {e}")

    out = (p.stdout or "") + "\n" + (p.stderr or "")
    if p.returncode != 0:
        # still may contain a valid gas estimate, caller decides
        return out, p.returncode
    return out, 0

def parse_gas(txt):
    m = GAS_RE.search(txt)
    if not m:
        raise ValueError("could not find 'gas estimate:' in CLI output")
    return int(m.group(1))

def ensure_csv(path: Path):
    if not path.exists():
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["round", "chars", "bytes_assumed", "gas_estimate"])

def last_completed_round(path: Path):
    if not path.exists():
        return -1
    last = -1
    with path.open("r", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                last = max(last, int(row["round"]))
            except Exception:
                pass
    return last

# ───────────────────────── MAIN ─────────────────────────
def main():
    csv_path = Path(CSV_PATH)
    ensure_csv(csv_path)
    start_round = last_completed_round(csv_path) + 1

    if start_round >= ROUNDS:
        print(f"[done] CSV already has {ROUNDS} rounds. Nothing to do.")
        return

    for rnd in range(start_round, ROUNDS):
        char_count = 1 << rnd   # 2^rnd
        data_payload = "a" * char_count
        bytes_assumed = char_count * 1

        # Write payload to a temp file to avoid argv size limits
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False) as tf:
                tf.write(data_payload)
                tmp_path = tf.name

            args = [
                FAIRYRINGD, "tx", "pep", "submit-encrypted-tx",
                TARGET_BLOCK_HEIGHT,                 # new form: <height> first
                f"--data-file={tmp_path}",           # payload via file
                "--from", FROM,
                "--keyring-backend", KEYRING_BACKEND,
                "--home", HOME_DIR,
                "--chain-id", CHAIN_ID,
                "--dry-run",
                "-y",
            ]

            print(f"[{rnd+1}/{ROUNDS}] chars={char_count:,} (assumed bytes={bytes_assumed:,}) ...", flush=True)
            combined_out, rc = run_cmd(args)
            gas = parse_gas(combined_out)

        except Exception as e:
            print(f"ERROR on round {rnd} (chars={char_count}): {e}", file=sys.stderr)
            snippet = combined_out.strip()[:20000] if 'combined_out' in locals() else ""
            if snippet:
                print("---- CLI output (first 20000 chars) ----", file=sys.stderr)
                print(snippet, file=sys.stderr)
                print("---- end output ----", file=sys.stderr)
            break
        finally:
            if tmp_path:
                try: os.remove(tmp_path)
                except OSError: pass

        with csv_path.open("a", newline="") as f:
            w = csv.writer(f)
            w.writerow([rnd, char_count, bytes_assumed, gas])

        print(f"  → gas_estimate={gas}", flush=True)
        time.sleep(SLEEP_BETWEEN_RUNS_SEC)

    print(f"Done. Results at: {csv_path.resolve()}")

if __name__ == "__main__":
    main()
