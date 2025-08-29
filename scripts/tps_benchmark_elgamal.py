#!/usr/bin/env python3

import os, sys, json, time, base64, subprocess, math, re, hashlib
from dataclasses import dataclass
from datetime import datetime

os.environ['PATH'] = 'GO_PATH:' + os.environ.get('PATH', '')

CHAIN_ID = "fairyring_devnet"
HOME_DIR = os.path.expanduser("HOME_DIR_PLACEHOLDER")
NODE = "tcp://localhost:26657"
COMMON = f"--chain-id {CHAIN_ID} --home {HOME_DIR} --node {NODE}"
BINARY = "fairyringd"
RPC = "http://localhost:26657"

DENOM = "ufairy"
GAS_PRICE = 0.0025
GAS_PER_TRANSFER = 200000
MAX_MSGS_PER_TX = 20000000

@dataclass
class BenchmarkResult:
    num_transfers: int
    tps: float
    block_time: float
    success_rate: float
    gas_used: int
    gas_per_tx: float
    errors: list
    single_block_achieved: bool
    actual_block_height: int

class ElGamalBenchmark:
    def __init__(self):
        self.contract_address = None
        self.token_address = None
        self.accounts = []
        self.account_names = []
        self.elgamal_data = None

        # Pre-funded test accounts
        self.funded_accounts = [
            {"name": "wallet1", "address": "fairy1m9l358xunhhwds0568za49mzhvuxx9uxdra8sq"},
            {"name": "wallet2", "address": "fairy10h9stc5v6ntgeygf5xf945njqq5h32r5lxfsqr"},
            {"name": "wallet3", "address": "fairy14zs2x38lmkw4eqvl3lpml5l8crzaxn6ms54wlt"},
            {"name": "wallet4", "address": "fairy1d84j42rnfgq60sjxpzj4pgfu35mew34d0mcyr6"},
            {"name": "wallet5", "address": "fairy10tq25z63m3fedlwmtssf5g5qzh9zsjswvmcxc9"},
            {"name": "wallet6", "address": "fairy1el5zya2muxh63qa2tsej802gvlf8um42cwpulh"},
            {"name": "val1",   "address": "fairy18hl5c9xn5dze2g50uaw0l2mr02ew57zkynp0td"},
        ]

        self.setup_env()
        self.elgamal_data = self.create_elgamal_data()

    def setup_env(self):
        r = subprocess.run([BINARY, "version"], capture_output=True, text=True)
        if r.returncode != 0:
            print("fairyringd not found in PATH")
            sys.exit(1)

    def run(self, cmd, input_text=None, timeout=None):
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, input=input_text, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(f"Command failed: {res.stderr.strip()}")
        return res.stdout.strip()

    def jrun(self, cmd, input_text=None, timeout=None):
        out = self.run(cmd, input_text, timeout)
        return json.loads(out)

    def rpc(self, path):
        return self.jrun(f"curl -s {RPC}{path}")

    @staticmethod
    def sha256_upper(data):
        return hashlib.sha256(data).hexdigest().upper()

    def find_tx_commit_via_blocks(self, start_height, txhash_upper, timeout=120.0, interval=0.5):
        """Scan new blocks until we find txhash"""
        deadline = time.time() + timeout
        next_scan = max(1, start_height)
        last_seen = start_height

        while time.time() < deadline:
            st = self.rpc("/status")
            latest = int(st["result"]["sync_info"]["latest_block_height"])
            if latest > last_seen:
                last_seen = latest

            while next_scan <= latest:
                blk = self.rpc(f"/block?height={next_scan}")
                txs = blk["result"]["block"]["data"].get("txs") or []
                for idx, tx_b64 in enumerate(txs):
                    try:
                        h = self.sha256_upper(base64.b64decode(tx_b64))
                    except Exception:
                        continue
                    if h == txhash_upper:
                        br = self.rpc(f"/block_results?height={next_scan}")
                        txr = br["result"]["txs_results"][idx]
                        return {
                            "height": next_scan,
                            "index": idx,
                            "code": int(txr.get("code", 0)),
                            "gas_used": int(txr.get("gas_used", 0)) if "gas_used" in txr else 0,
                            "codespace": txr.get("codespace", ""),
                            "log": txr.get("log", ""),
                            "events": txr.get("events", []),
                        }
                next_scan += 1
            time.sleep(interval)

        raise RuntimeError(f"Timed out scanning blocks for tx {txhash_upper}")


    def broadcast_sync_and_wait(self, signed_path):
        st = self.rpc("/status")
        start_height = int(st["result"]["sync_info"]["latest_block_height"])
        res = self.jrun(f"{BINARY} tx broadcast {signed_path} --broadcast-mode=sync --output json {COMMON}")
        if res.get("code", 0) != 0:
            raise RuntimeError(f"Broadcast (CheckTx) failed code={res.get('code')}: {res.get('raw_log')}")
        txhash = res["txhash"].upper()

        until = time.time() + 5
        while time.time() < until:
            try:
                q = self.jrun(f"{BINARY} q tx {txhash} --output json {COMMON}")
                if int(q.get("height", "0")) > 0:
                    q["txhash"] = txhash
                    q.setdefault("codespace", q.get("codespace",""))
                    q.setdefault("raw_log", q.get("raw_log",""))
                    return q
            except Exception:
                pass
            time.sleep(0.5)

        info = self.find_tx_commit_via_blocks(start_height + 1, txhash)
        return {
            "txhash": txhash,
            "height": str(info["height"]),
            "code": info["code"],
            "gas_used": info["gas_used"],
            "codespace": info.get("codespace",""),
            "raw_log": info.get("log",""),
            "events": info.get("events", []),
        }


    def tx(self, cmd):
        """Execute a single-message tx in sync mode, then wait for commit"""
        try:
            if "-y" not in cmd: cmd += " -y"
            if "--output json" not in cmd: cmd += " --output json"
            if "--broadcast-mode" not in cmd: cmd += " --broadcast-mode=sync"
            if "--chain-id" not in cmd: cmd += f" --chain-id {CHAIN_ID}"
            if "--home" not in cmd: cmd += f" --home {HOME_DIR}"
            if "--node" not in cmd: cmd += f" --node {NODE}"
            if not any(x in cmd for x in ("--gas ", "--fees", "--gas-prices")):
                cmd += " --gas 300000"

            st = self.rpc("/status")
            start_height = int(st["result"]["sync_info"]["latest_block_height"])

            res = self.jrun(cmd)
            if res.get("code", 0) != 0:
                raise RuntimeError(f"Broadcast (CheckTx) failed code={res.get('code')}: {res.get('raw_log')}")
            txhash = res["txhash"].upper()

            quick_deadline = time.time() + 5.0
            while time.time() < quick_deadline:
                try:
                    q = self.jrun(f"{BINARY} q tx {txhash} --output json {COMMON}")
                    if int(q.get("height", "0")) > 0:
                        return q
                except Exception:
                    pass
                time.sleep(0.5)

            info = self.find_tx_commit_via_blocks(start_height + 1, txhash)
            return {
                "txhash": txhash,
                "height": str(info["height"]),
                "code": info["code"],
                "gas_used": info["gas_used"],
                "codespace": info.get("codespace",""),
                "raw_log": info.get("log",""),
                "events": info.get("events", []),
            }
        except Exception as e:
            print(f"Transaction failed: {e}")
            return None


    def exec_tx(self, msg, from_account):
        js = json.dumps(msg)
        if "deposit" in js:
            gas_limit = "500000"
        elif "transfer" in js:
            gas_limit = "1800000"
        elif "withdraw" in js:
            gas_limit = "800000"
        else:
            gas_limit = "300000"
        return self.tx(f"{BINARY} tx wasm execute {self.contract_address} '{js}' --from {from_account} --gas {gas_limit} {COMMON}")

    def create_elgamal_data(self):
        PROOF_GENERATOR_DIR = "PROOF_GENERATOR_DIR_PLACEHOLDER"
        def pg(cmd):
            full_cmd = f"cd {PROOF_GENERATOR_DIR} && {cmd}"
            return subprocess.check_output(full_cmd, shell=True).decode()

        keypair = json.loads(pg("proof-generator generate-keypair"))
        public_key = keypair["public_key"]
        private_key = keypair["private_key"]

        transfer_enc = json.loads(pg(f"proof-generator encrypt --amount 2 --pubkey {public_key}"))
        transfer_commitment = transfer_enc.get('commitment', '')
        transfer_handle = transfer_enc.get('handle', '')

        deposit_enc = json.loads(pg(f"proof-generator encrypt --amount 100000 --pubkey {public_key}"))
        deposit_commitment = deposit_enc.get('commitment', '')
        deposit_handle = deposit_enc.get('handle', '')

        placeholder_proof = base64.b64encode(b"placeholder_proof_data").decode()

        return {
            "elgamal_pubkey": {"key": public_key},
            "elgamal_private_key": private_key,
            "encrypted_amount": {"c1": transfer_commitment, "c2": transfer_handle},
            "deposit_encrypted_amount": {"c1": deposit_commitment, "c2": deposit_handle},
            "transfer_proof_data": {
                "equality_proof_data": placeholder_proof,
                "transfer_amount_ciphertext_validity_proof_data_with_ciphertext": placeholder_proof,
                "percentage_with_cap_proof_data": placeholder_proof,
                "fee_ciphertext_validity_proof_data": placeholder_proof,
                "range_proof_data": placeholder_proof
            }
        }

    def smart_query(self, query_msg):
        raw = self.jrun(f"{BINARY} query wasm contract-state smart {self.contract_address} '{json.dumps(query_msg)}' --output json {COMMON}")
        payload = raw.get("data", raw)
        if isinstance(payload, str):
            try:
                payload = json.loads(base64.b64decode(payload).decode())
            except Exception:
                pass
        return payload

    def try_decrypt(self, sk_b64, cipher, label):
        try:
            PROOF_GENERATOR_DIR = "PROOF_GENERATOR_DIR_PLACEHOLDER"
            
            c1_bytes = base64.b64decode(cipher["c1"])
            c2_bytes = base64.b64decode(cipher["c2"])
            combined_bytes = c1_bytes + c2_bytes
            ciphertext_b64 = base64.b64encode(combined_bytes).decode('utf-8')
            
            full_cmd = f"cd {PROOF_GENERATOR_DIR} && proof-generator decrypt --ciphertext {ciphertext_b64} --keypair {sk_b64}"
            decrypt_output = subprocess.check_output(full_cmd, shell=True).decode()
            decrypt_result = json.loads(decrypt_output)
            return decrypt_result["decrypted_amount"]
        except Exception:
            return None

    def get_account_balances(self, sender_addr, recipient_addr):
        sender_acct = self.smart_query({"get_account": {"owner": sender_addr}})
        recipient_acct = self.smart_query({"get_account": {"owner": recipient_addr}})
        
        private_key = self.elgamal_data.get("elgamal_private_key", "")
        
        sender_available = self.try_decrypt(private_key, sender_acct["available_balance"], "sender.available")
        sender_pending = self.try_decrypt(private_key, sender_acct["pending_balance"], "sender.pending")
        recipient_available = self.try_decrypt(private_key, recipient_acct["available_balance"], "recipient.available")
        recipient_pending = self.try_decrypt(private_key, recipient_acct["pending_balance"], "recipient.pending")
        
        return {
            "sender": {
                "available": sender_available,
                "pending": sender_pending,
                "total": (sender_available or 0) + (sender_pending or 0)
            },
            "recipient": {
                "available": recipient_available,
                "pending": recipient_pending,
                "total": (recipient_available or 0) + (recipient_pending or 0)
            }
        }




    def _q_wasm_list_code(self):
        q = self.jrun(f"{BINARY} q wasm list-code --output json {COMMON}")
        return q.get("code_infos") or q.get("codeInfos") or []

    def _latest_code_id(self):
        infos = self._q_wasm_list_code()
        max_id = 0
        for ci in infos:
            cid = int(ci.get("code_id") or ci.get("codeId") or 0)
            if cid > max_id: max_id = cid
        return max_id or None

    def _list_contracts_by_code(self, code_id):
        q = self.jrun(f"{BINARY} q wasm list-contract-by-code {code_id} --output json {COMMON}")
        addrs = q.get("contracts") or []
        return addrs

    def deploy_contracts(self):
        print("Deploying contracts...")
        self.run("./deploy.sh")

        wasm_file = 'artifacts/elgamal_mvp_fixed.wasm'
        cw20_wasm = 'artifacts/cw20_base.wasm'

        prev_code_top = self._latest_code_id() or 0

        self.tx(f"{BINARY} tx wasm store {cw20_wasm} --from wallet1 {COMMON} --gas 3000000 --fees 3000000{DENOM}")

        time.sleep(0.5)
        cw20_code_id = None
        for _ in range(20):
            cur_top = self._latest_code_id() or 0
            if cur_top > prev_code_top:
                cw20_code_id = cur_top
                break
            time.sleep(0.5)

        cw20_init = {
            "name": "ElgamalToken",
            "symbol": "EGM",
            "decimals": 6,
            "initial_balances": [{"address": self.funded_accounts[0]["address"], "amount": "1000000000"}],
            "mint": {"minter": self.funded_accounts[0]["address"]}
        }
        self.tx(f"{BINARY} tx wasm instantiate {cw20_code_id} '{json.dumps(cw20_init)}' --no-admin --from wallet1 {COMMON} --gas 3000000 --fees 3000000{DENOM} --label cw20-base")

        time.sleep(0.5)
        addrs = []
        for _ in range(20):
            addrs = self._list_contracts_by_code(cw20_code_id)
            if addrs: break
            time.sleep(0.5)
        self.token_address = addrs[-1]
        print(f"CW20 address: {self.token_address}")

        prev_code_top = self._latest_code_id() or cw20_code_id
        self.tx(f"{BINARY} tx wasm store {wasm_file} --from wallet1 {COMMON} --gas 6000000 --fees 6000000{DENOM}")

        elgamal_code_id = None
        for _ in range(20):
            cur_top = self._latest_code_id() or 0
            if cur_top > prev_code_top:
                elgamal_code_id = cur_top
                break
            time.sleep(0.5)

        elgamal_init = {
            "mint_encryption_key": {"key": self.elgamal_data["elgamal_pubkey"]["key"]},
            "token_address": self.token_address
        }
        self.tx(f"{BINARY} tx wasm instantiate {elgamal_code_id} '{json.dumps(elgamal_init)}' --no-admin --from wallet1 {COMMON} --gas 300000 --label elgamal-demo")

        addrs = []
        for _ in range(20):
            addrs = self._list_contracts_by_code(elgamal_code_id)
            if addrs: break
            time.sleep(0.5)
        self.contract_address = addrs[-1]
        print(f"ElGamal address: {self.contract_address}")


    def exec_setup(self, account_name, account_addr):
        transfer_msg = {"transfer": {"recipient": account_addr, "amount": "10000000"}}
        self.tx(f"{BINARY} tx wasm execute {self.token_address} '{json.dumps(transfer_msg)}' --from wallet1 --gas 300000 {COMMON}")

        allowance_msg = {"increase_allowance": {"spender": self.contract_address, "amount": "5000000"}}
        self.tx(f"{BINARY} tx wasm execute {self.token_address} '{json.dumps(allowance_msg)}' --from {account_name} --gas 300000 {COMMON}")

        create_msg = {"create_confidential_account": {"owner": account_addr, "elgamal_pubkey": self.elgamal_data["elgamal_pubkey"]}}
        self.exec_tx(create_msg, account_name)

        deposit_msg = {"deposit": {"owner": account_addr, "plain_amount": "100000", "encrypted_amount": self.elgamal_data["deposit_encrypted_amount"]}}
        self.exec_tx(deposit_msg, account_name)

        apply_msg = {"apply_pending": {"owner": account_addr}}
        self.exec_tx(apply_msg, account_name)

    def setup_accounts(self, num_accounts):
        print("Setting up accounts...")
        self.accounts, self.account_names = [], []
        for i in range(min(num_accounts, len(self.funded_accounts))):
            acc = self.funded_accounts[i]
            self.exec_setup(acc["name"], acc["address"])
            self.accounts.append(acc["address"])
            self.account_names.append(acc["name"])
        
        if "wallet3" not in self.account_names and len(self.funded_accounts) >= 3:
            wallet3_acc = self.funded_accounts[2]
            self.exec_setup(wallet3_acc["name"], wallet3_acc["address"])
            self.accounts.append(wallet3_acc["address"])
            self.account_names.append(wallet3_acc["name"])
        
        if "wallet4" not in self.account_names and len(self.funded_accounts) >= 4:
            wallet4_acc = self.funded_accounts[3]
            self.exec_setup(wallet4_acc["name"], wallet4_acc["address"])
            self.accounts.append(wallet4_acc["address"])
            self.account_names.append(wallet4_acc["name"])
        
        if "wallet5" not in self.account_names and len(self.funded_accounts) >= 5:
            wallet5_acc = self.funded_accounts[4]
            self.exec_setup(wallet5_acc["name"], wallet5_acc["address"])
            self.accounts.append(wallet5_acc["address"])
            self.account_names.append(wallet5_acc["name"])
        
        if "wallet6" not in self.account_names and len(self.funded_accounts) >= 6:
            wallet6_acc = self.funded_accounts[5]
            self.exec_setup(wallet6_acc["name"], wallet6_acc["address"])
            self.accounts.append(wallet6_acc["address"])
            self.account_names.append(wallet6_acc["name"])
        
        print(f"Setup {len(self.accounts)} accounts")


    def get_block_info_by_height(self, height):
        data = self.rpc(f"/block?height={height}")
        return {"height": int(data["result"]["block"]["header"]["height"]),
                "timestamp": data["result"]["block"]["header"]["time"]}

    @staticmethod
    def _parse_rfc3339(ts):
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.(\d+))?([+-]\d{2}:\d{2})$", ts)
        if not m:
            return datetime.fromisoformat(ts)
        head, _, frac, tz = m.groups()
        frac = (frac or "")
        if len(frac) > 6: frac = frac[:6]
        elif len(frac) < 6: frac = (frac + "000000")[:6] if frac else "000000"
        return datetime.fromisoformat(f"{head}.{frac}{tz}")

    def block_time(self, height):
        if height <= 1: return 1.0
        t1 = self._parse_rfc3339(self.get_block_info_by_height(height)["timestamp"])
        t0 = self._parse_rfc3339(self.get_block_info_by_height(height-1)["timestamp"])
        dt = (t1 - t0).total_seconds()
        return dt if dt > 0 else 1.0


    def _unsigned_skeleton(self, contract, msg_obj, sender_name, memo, gas_limit, fee_amount):
        """Build unsigned tx skeleton with one message using --generate-only"""
        cmd = (f"{BINARY} tx wasm execute {contract} '{json.dumps(msg_obj)}' "
               f"--from {sender_name} --gas {gas_limit} --fees {fee_amount}{DENOM} "
               f"--generate-only  --output json {COMMON}")
        unsigned = self.jrun(cmd)

        if not unsigned.get("body") or not unsigned["body"].get("messages"):
            raise RuntimeError("Unexpected unsigned tx shape from --generate-only")

        m0 = unsigned["body"]["messages"][0]
        if not isinstance(m0.get("funds"), list):
            m0["funds"] = []

        unsigned["body"]["messages"][0] = m0
        unsigned["auth_info"]["fee"]["gas_limit"] = str(gas_limit)
        unsigned["auth_info"]["fee"]["amount"] = [{"denom": DENOM, "amount": str(fee_amount)}]
        return unsigned

    def _append_execute_msgs(self, unsigned, contract, msg_objs):
        """Append additional MsgExecuteContract messages"""
        tmpl = unsigned["body"]["messages"][0]
        sender = tmpl["sender"]
        funds = tmpl.get("funds", [])
        for m in msg_objs:
            unsigned["body"]["messages"].append({
                "@type": "/cosmwasm.wasm.v1.MsgExecuteContract",
                "sender": sender,
                "contract": contract,
                "msg": m,
                "funds": funds if isinstance(funds, list) else []
            })

    def _sign(self, unsigned_path, signer):
        return self.jrun(f"{BINARY} tx sign {unsigned_path} --from {signer} {COMMON} --output json")


    def execute_single_block_transfers(self, num_transfers):
        if len(self.accounts) < 5:
            self.setup_accounts(5)

        sender_addr = self.accounts[0]
        recipient_addr = self.accounts[1]
        sender_name = self.account_names[0]
        
        concurrent_sender1_addr = self.accounts[2] if len(self.accounts) > 2 else self.accounts[0]
        concurrent_sender1_name = self.account_names[2] if len(self.account_names) > 2 else self.account_names[0]
        concurrent_sender2_addr = self.accounts[3] if len(self.accounts) > 3 else self.accounts[0]
        concurrent_sender2_name = self.account_names[3] if len(self.account_names) > 3 else self.account_names[0]
        concurrent_sender3_addr = self.accounts[4] if len(self.accounts) > 4 else self.accounts[0]
        concurrent_sender3_name = self.account_names[4] if len(self.account_names) > 4 else self.account_names[0]
        concurrent_sender4_addr = self.accounts[5] if len(self.accounts) > 5 else self.accounts[0]
        concurrent_sender4_name = self.account_names[5] if len(self.account_names) > 5 else self.account_names[0]

        if num_transfers > MAX_MSGS_PER_TX:
            return False, 1.0, 0, [f"Requested {num_transfers} > MAX_MSGS_PER_TX={MAX_MSGS_PER_TX}"], 0

        all_sender_balances_before = {}
        sender_addresses = [sender_addr, concurrent_sender1_addr, concurrent_sender2_addr, concurrent_sender3_addr, concurrent_sender4_addr]
        sender_names = [sender_name, concurrent_sender1_name, concurrent_sender2_name, concurrent_sender3_name, concurrent_sender4_name]
        
        recipient_balances = self.get_account_balances(sender_addr, recipient_addr)
        
        for addr, name in zip(sender_addresses, sender_names):
            sender_balances = self.get_account_balances(addr, recipient_addr)
            all_sender_balances_before[name] = sender_balances["sender"]["total"]

        msgs = []
        concurrent_msgs1 = []
        concurrent_msgs2 = []
        concurrent_msgs3 = []
        concurrent_msgs4 = []
        for _ in range(num_transfers):
            msgs.append({
                "transfer_confidential": {
                    "sender": sender_addr,
                    "recipient": recipient_addr,
                    "encrypted_amount": self.elgamal_data["encrypted_amount"],
                    "transfer_proof_data": self.elgamal_data["transfer_proof_data"]
                }
            })
            concurrent_msgs1.append({
                "transfer_confidential": {
                    "sender": concurrent_sender1_addr,
                    "recipient": recipient_addr,
                    "encrypted_amount": self.elgamal_data["encrypted_amount"],
                    "transfer_proof_data": self.elgamal_data["transfer_proof_data"]
                }
            })
            concurrent_msgs2.append({
                "transfer_confidential": {
                    "sender": concurrent_sender2_addr,
                    "recipient": recipient_addr,
                    "encrypted_amount": self.elgamal_data["encrypted_amount"],
                    "transfer_proof_data": self.elgamal_data["transfer_proof_data"]
                }
            })
            concurrent_msgs3.append({
                "transfer_confidential": {
                    "sender": concurrent_sender3_addr,
                    "recipient": recipient_addr,
                    "encrypted_amount": self.elgamal_data["encrypted_amount"],
                    "transfer_proof_data": self.elgamal_data["transfer_proof_data"]
                }
            })
            concurrent_msgs4.append({
                "transfer_confidential": {
                    "sender": concurrent_sender4_addr,
                    "recipient": recipient_addr,
                    "encrypted_amount": self.elgamal_data["encrypted_amount"],
                    "transfer_proof_data": self.elgamal_data["transfer_proof_data"]
                }
            })

        gas_limit = GAS_PER_TRANSFER * num_transfers
        fee_amount = math.ceil(gas_limit * GAS_PRICE)
        memo = f"TPS benchmark: {num_transfers * 5} transfers (5 concurrent tx)"

        gas_limit = int(gas_limit * 1.2)
        fee_amount = math.ceil(gas_limit * GAS_PRICE)

        max_reasonable_gas = 1000000000
        if gas_limit > max_reasonable_gas:
            gas_limit = max_reasonable_gas
            fee_amount = math.ceil(gas_limit * GAS_PRICE)
        unsigned = self._unsigned_skeleton(self.contract_address, msgs[0], sender_name, memo, gas_limit, fee_amount)
        if num_transfers > 1:
            self._append_execute_msgs(unsigned, self.contract_address, msgs[1:])

        unsigned_path = "/tmp/unsigned_multi.json"
        with open(unsigned_path, "w") as f:
            json.dump(unsigned, f, separators=(",", ":"), ensure_ascii=False)

        signed = self._sign(unsigned_path, sender_name)
        signed_path = "/tmp/signed_multi.json"
        with open(signed_path, "w") as f:
            json.dump(signed, f, separators=(",", ":"), ensure_ascii=False)



        concurrent_unsigned1 = self._unsigned_skeleton(self.contract_address, concurrent_msgs1[0], concurrent_sender1_name, memo, gas_limit, fee_amount)
        if num_transfers > 1:
            self._append_execute_msgs(concurrent_unsigned1, self.contract_address, concurrent_msgs1[1:])

        concurrent_unsigned1_path = "/tmp/concurrent1_unsigned_multi.json"
        with open(concurrent_unsigned1_path, "w") as f:
            json.dump(concurrent_unsigned1, f, separators=(",", ":"), ensure_ascii=False)

        concurrent_signed1 = self._sign(concurrent_unsigned1_path, concurrent_sender1_name)
        concurrent_signed1_path = "/tmp/concurrent1_signed_multi.json"
        with open(concurrent_signed1_path, "w") as f:
            json.dump(concurrent_signed1, f, separators=(",", ":"), ensure_ascii=False)

        concurrent_unsigned2 = self._unsigned_skeleton(self.contract_address, concurrent_msgs2[0], concurrent_sender2_name, memo, gas_limit, fee_amount)
        if num_transfers > 1:
            self._append_execute_msgs(concurrent_unsigned2, self.contract_address, concurrent_msgs2[1:])

        concurrent_unsigned2_path = "/tmp/concurrent2_unsigned_multi.json"
        with open(concurrent_unsigned2_path, "w") as f:
            json.dump(concurrent_unsigned2, f, separators=(",", ":"), ensure_ascii=False)

        concurrent_signed2 = self._sign(concurrent_unsigned2_path, concurrent_sender2_name)
        concurrent_signed2_path = "/tmp/concurrent2_signed_multi.json"
        with open(concurrent_signed2_path, "w") as f:
            json.dump(concurrent_signed2, f, separators=(",", ":"), ensure_ascii=False)

        concurrent_unsigned3 = self._unsigned_skeleton(self.contract_address, concurrent_msgs3[0], concurrent_sender3_name, memo, gas_limit, fee_amount)
        if num_transfers > 1:
            self._append_execute_msgs(concurrent_unsigned3, self.contract_address, concurrent_msgs3[1:])

        concurrent_unsigned3_path = "/tmp/concurrent3_unsigned_multi.json"
        with open(concurrent_unsigned3_path, "w") as f:
            json.dump(concurrent_unsigned3, f, separators=(",", ":"), ensure_ascii=False)

        concurrent_signed3 = self._sign(concurrent_unsigned3_path, concurrent_sender3_name)
        concurrent_signed3_path = "/tmp/concurrent3_signed_multi.json"
        with open(concurrent_signed3_path, "w") as f:
            json.dump(concurrent_signed3, f, separators=(",", ":"), ensure_ascii=False)

        concurrent_unsigned4 = self._unsigned_skeleton(self.contract_address, concurrent_msgs4[0], concurrent_sender4_name, memo, gas_limit, fee_amount)
        if num_transfers > 1:
            self._append_execute_msgs(concurrent_unsigned4, self.contract_address, concurrent_msgs4[1:])

        concurrent_unsigned4_path = "/tmp/concurrent4_unsigned_multi.json"
        with open(concurrent_unsigned4_path, "w") as f:
            json.dump(concurrent_unsigned4, f, separators=(",", ":"), ensure_ascii=False)

        concurrent_signed4 = self._sign(concurrent_unsigned4_path, concurrent_sender4_name)
        concurrent_signed4_path = "/tmp/concurrent4_signed_multi.json"
        with open(concurrent_signed4_path, "w") as f:
            json.dump(concurrent_signed4, f, separators=(",", ":"), ensure_ascii=False)


        
        try:
            import threading
            import queue
            
            results_queue = queue.Queue()
            
            def broadcast_transaction(signed_path, sender_name, is_concurrent=False):
                try:
                    q = self.broadcast_sync_and_wait(signed_path)
                    results_queue.put((sender_name, q, None))
                except Exception as e:
                    results_queue.put((sender_name, None, e))
            
            thread1 = threading.Thread(target=broadcast_transaction, args=(signed_path, sender_name, False))
            thread2 = threading.Thread(target=broadcast_transaction, args=(concurrent_signed1_path, concurrent_sender1_name, True))
            thread3 = threading.Thread(target=broadcast_transaction, args=(concurrent_signed2_path, concurrent_sender2_name, True))
            thread4 = threading.Thread(target=broadcast_transaction, args=(concurrent_signed3_path, concurrent_sender3_name, True))
            thread5 = threading.Thread(target=broadcast_transaction, args=(concurrent_signed4_path, concurrent_sender4_name, True))
            
            thread1.start()
            thread2.start()
            thread3.start()
            thread4.start()
            thread5.start()
            
            thread1.join()
            thread2.join()
            thread3.join()
            thread4.join()
            thread5.join()
            
            results = {}
            errors = {}
            while not results_queue.empty():
                sender, result, error = results_queue.get()
                if error:
                    errors[sender] = error
                else:
                    results[sender] = result
            
            if len(results) != 5:
                return False, 1.0, 0, [f"Concurrent transaction failed: {list(errors.values())}"], 0
            
            q = results[sender_name]
            concurrent_q1 = results[concurrent_sender1_name]
            concurrent_q2 = results[concurrent_sender2_name]
            concurrent_q3 = results[concurrent_sender3_name]
            concurrent_q4 = results[concurrent_sender4_name]
            
            height = int(q.get("height", 0))
            concurrent_height1 = int(concurrent_q1.get("height", 0))
            concurrent_height2 = int(concurrent_q2.get("height", 0))
            concurrent_height3 = int(concurrent_q3.get("height", 0))
            concurrent_height4 = int(concurrent_q4.get("height", 0))

            if height != concurrent_height1 or height != concurrent_height2 or height != concurrent_height3 or height != concurrent_height4:
                return False, 1.0, 0, [f"Transactions not in same block: {height} vs {concurrent_height1} vs {concurrent_height2} vs {concurrent_height3} vs {concurrent_height4}"], 0
            
            gas_used = int(q.get("gas_used", 0)) if q.get("gas_used") is not None else 0
            concurrent_gas_used1 = int(concurrent_q1.get("gas_used", 0)) if concurrent_q1.get("gas_used") is not None else 0
            concurrent_gas_used2 = int(concurrent_q2.get("gas_used", 0)) if concurrent_q2.get("gas_used") is not None else 0
            concurrent_gas_used3 = int(concurrent_q3.get("gas_used", 0)) if concurrent_q3.get("gas_used") is not None else 0
            concurrent_gas_used4 = int(concurrent_q4.get("gas_used", 0)) if concurrent_q4.get("gas_used") is not None else 0
            total_gas_used = gas_used + concurrent_gas_used1 + concurrent_gas_used2 + concurrent_gas_used3 + concurrent_gas_used4
            
            code = int(q.get("code", 0))
            height = int(q.get("height", 0))
            
            if code != 0:
                return False, 1.0, total_gas_used, [f"On-chain failure code={code}: {q.get('codespace','')}: {q.get('raw_log','')}"], height

            bt = self.block_time(height)
            total_transfer_messages = num_transfers * 5
            gas_per_tx = total_gas_used / total_transfer_messages if total_transfer_messages > 0 else 0
            
            recipient_balances_after = self.get_account_balances(sender_addr, recipient_addr)
            recipient_change = recipient_balances_after["recipient"]["total"] - recipient_balances["recipient"]["total"]
            
            all_sender_balances_after = {}
            sender_addresses = [sender_addr, concurrent_sender1_addr, concurrent_sender2_addr, concurrent_sender3_addr, concurrent_sender4_addr]
            sender_names = [sender_name, concurrent_sender1_name, concurrent_sender2_name, concurrent_sender3_name, concurrent_sender4_name]
            
            total_sender_change = 0
            for addr, name in zip(sender_addresses, sender_names):
                sender_balances_after = self.get_account_balances(addr, recipient_addr)
                all_sender_balances_after[name] = sender_balances_after["sender"]["total"]
                balance_change = sender_balances_after["sender"]["total"] - all_sender_balances_before[name]
                total_sender_change += balance_change
            
            expected_sender_change = -num_transfers * 2 * 5
            expected_recipient_change = num_transfers * 2 * 5
            
            if total_sender_change == expected_sender_change and recipient_change == expected_recipient_change:
                return True, bt, total_gas_used, [], height
            else:
                return False, 1.0, total_gas_used, ["Transfer verification failed"], height
            
        except Exception as e:
            return False, 1.0, 0, [str(e)], 0

    def run_benchmark(self, num_transfers):
        if not self.contract_address or not self.token_address:
            print("Contracts not found, deploying...")
            self.deploy_contracts()

        success, block_time, gas_used, errors, execution_height = self.execute_single_block_transfers(num_transfers)
        total_transfer_messages = num_transfers * 5
        tps = (total_transfer_messages / block_time) if (success and block_time > 0) else 0.0
        gas_per = (gas_used / total_transfer_messages) if (success and total_transfer_messages > 0 and gas_used) else 0.0

        return BenchmarkResult(
            num_transfers=total_transfer_messages,
            tps=tps,
            block_time=block_time,
            success_rate=100.0 if success else 0.0,
            gas_used=gas_used,
            gas_per_tx=gas_per,
            errors=errors,
            single_block_achieved=success,
            actual_block_height=execution_height
        )

def main():
    b = ElGamalBenchmark()

    if os.environ.get("CW20_CONTRACT") and os.environ.get("ELGAMAL_CONTRACT"):
        b.token_address = os.environ["CW20_CONTRACT"]
        b.contract_address = os.environ["ELGAMAL_CONTRACT"]

    if len(sys.argv) > 1:
        try:
            num_transfers = int(sys.argv[1])
            test_volumes = [num_transfers]
        except ValueError:
            print(f"Invalid number of transfers: {sys.argv[1]}")
            sys.exit(1)
    else:
        test_volumes = [10,100,500,1205]
    
    results = []

    for n in test_volumes:
        try:
            r = b.run_benchmark(n)
            results.append(r)
            status = "YES" if r.single_block_achieved else "NO"
            print(f"{n:,} transfers per tx (total: {n*5:,}) -> TPS={r.tps:.2f}, Block time={r.block_time:.3f}s, Single block={status}")
            if r.errors: print("   Errors:", r.errors)
        except Exception as e:
            print(f"ERROR testing {n:,}: {e}")
            results.append(BenchmarkResult(
                num_transfers=n*5, tps=0.0, block_time=0.0, success_rate=0.0,
                gas_used=0, gas_per_tx=0.0, errors=[str(e)],
                single_block_achieved=False, actual_block_height=0
            ))

    print("\n" + "=" * 80)
    print("FINAL RESULTS")
    print("=" * 80)
    print("Transfers | TPS      | Block Time | Success % | Gas/Tx    | Single Block | Height")
    print("--------- | -------- | ---------- | --------- | --------- | ------------ | ------")
    for r in results:
        sb = "YES" if r.single_block_achieved else "NO"
        print(f"{r.num_transfers:8,} | {r.tps:7.2f} | {r.block_time:10.3f}s | {r.success_rate:8.0f}% | {r.gas_per_tx:8.0f} | {sb:12} | {r.actual_block_height}")

if __name__ == "__main__":
    main()
