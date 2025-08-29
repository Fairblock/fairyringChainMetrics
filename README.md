# ConfidentialComputingBenchmark

Instructions for running benchmark tests against a local **Fairyring** devnet. This repository currently supports the following benchmark flows:

1. **Bank transfer benchmark** (Cosmos SDK `bank send`) — `scripts/bank_send_step.py`
2. **IBE-MPC benchmark** — Go CLI (`benchmark`)

> **Important:** The Fairyring devnet **must be restarted after each benchmark** to ensure clean, comparable results.

---

## 1. Prerequisites

- **Operating System:** Linux or macOS
- **Dependencies:**
  - **Git**
  - **Go** (≥ 1.21 recommended)
  - **Make**
  - **Python** (≥ 3.10)
  - *(Optional)* `jq` for JSON output formatting during sanity checks

---

## 2. Start the Fairyring Devnet

The benchmarks assume a locally running Fairyring devnet.

1. **Clone and prepare the chain repository (benchmark branch):**

   ```bash
   git clone https://github.com/Fairblock/fairyring
   cd fairyring
   git checkout benchmark
   ```

2. **Build and install the chain binaries:**

   ```bash
   go mod tidy
   make install
   ```

3. **Start the devnet:**

   ```bash
   make devnet-up
   ```

4. **Verify the node is producing blocks (height should increase):**

   ```bash
   # Option A
   curl -s http://127.0.0.1:26657/status | jq '.result.sync_info.latest_block_height'

   # Option B
   fairyringd status | jq '.SyncInfo.latest_block_height'
   ```

> **Stop the devnet** with:
>
> ```bash
> make devnet-down
> ```
>
> You **must** restart the devnet **after finishing each benchmark**:
>
> ```bash
> make devnet-down && make devnet-up
> ```

---

## 3. Configure Local Paths (HOME_DIR)

Both benchmarks expect the devnet home directory at:

```bash
<path_to_fairyring_checkout>/devnet_data/fairyring_devnet
```

Update the hard-coded paths **before running any tests**.

### 3.1 Bank benchmark (Python)

Edit `scripts/bank_send_step.py` and set:

```python
HOME_DIR   = "<path_to_fairyring_checkout>/devnet_data/fairyring_devnet"
```

### 3.2 IBE benchmark (Go)

Edit `ibe_benchmark.go` and set:

```go
homeDir    = "<path_to_fairyring_checkout>/devnet_data/fairyring_devnet"
```

#### Optional: one‑liners to update paths

> Replace `FAIRYRING_CHECKOUT` with your absolute path to the `fairyring` repository.

**Linux (GNU sed):**

```bash
export FAIRYRING_CHECKOUT="/absolute/path/to/fairyring"
sed -i "s|HOME_DIR\\s*=\\s*\\\".*\\\"|HOME_DIR   = \\\"$FAIRYRING_CHECKOUT/devnet_data/fairyring_devnet\\\"|" scripts/bank_send_step.py
sed -i "s|homeDir\\s*=\\s*\\\".*\\\"|homeDir    = \\\"$FAIRYRING_CHECKOUT/devnet_data/fairyring_devnet\\\"|" ibe_benchmark.go
```

**macOS (BSD sed):**

```bash
export FAIRYRING_CHECKOUT="/absolute/path/to/fairyring"
sed -i '' "s|HOME_DIR\\s*=\\s*\\\".*\\\"|HOME_DIR   = \\\"$FAIRYRING_CHECKOUT/devnet_data/fairyring_devnet\\\"|" scripts/bank_send_step.py
sed -i '' "s|homeDir\\s*=\\s*\\\".*\\\"|homeDir    = \\\"$FAIRYRING_CHECKOUT/devnet_data/fairyring_devnet\\\"|" ibe_benchmark.go
```

---

## 4. Benchmark A — Bank Transfers

**Script:** `scripts/bank_send_step.py`

This benchmark measures throughput and gas for standard Cosmos SDK **bank transfer** transactions (`bank send`) against the running devnet.

### 4.1 Run

From the repository root:

```bash
python3 scripts/bank_send_step.py
```

### 4.2 Configuration notes

Review and adjust top‑of‑file constants in `scripts/bank_send_step.py` as needed (e.g., `CHAIN_ID`, `KEYRING`, `DENOM`, `GAS_PRICE`, `NODE`, and any step/size controls). Ensure `HOME_DIR` is set as described in Section 3.

### 4.3 Outputs

- Console output includes per‑round summaries (counts, latency, gas, and errors if any).
- If the script writes CSV/JSON artifacts, they will be created alongside the script (refer to in‑file comments for exact paths/names).

> **After completion**, restart the devnet:
>
> ```bash
> (inside your `fairyring` checkout)
> make devnet-down && make devnet-up
> ```

---

## 5. Benchmark B — IBE Transactions

**CLI:** `benchmark`

This benchmark submits **IBE transactions** and records associated metrics.

### 5.1 Build the CLI

From this repository’s root:

```bash
go mod tidy
go install
```

Ensure your Go bin directory is on `PATH`:

```bash
export PATH="$(go env GOPATH)/bin:$PATH"
```

### 5.2 Run

```bash
benchmark
```

The command will use the `homeDir` value configured in `ibe_benchmark.go`. If your local branch adds flags (e.g., custom chain ID or RPC endpoint), consult `benchmark --help`.

### 5.3 Outputs

- Console output will display progress and per‑round metrics.
- If CSV/JSON files are produced, they will be written to the directory configured in your Go code (e.g., `recorded_stats/`, depending on the exact implementation).

> **After completion**, restart the devnet:
>
> ```bash
> (inside your `fairyring` checkout)
> make devnet-down && make devnet-up
> ```

---

## 6. Sanity Checks and Troubleshooting

- **Devnet not producing blocks**  
  Re‑run `make devnet-up` in the `fairyring` repository; verify with `fairyringd status` or the RPC `status` endpoint. Confirm ports `26657/26656/1317` are not in use by other processes.

- **CLI not found (`benchmark`)**  
  Ensure the Go install path is on `PATH` (see Section 5.1).

- **Incorrect `HOME_DIR/homeDir`**  
  Errors involving keys/accounts or missing data typically indicate a wrong devnet home path. Revisit Section 3.

- **Wallets or funding in devnet**  
  The `benchmark` branch provisions default keys and balances (e.g., `wallet1`). If you have customized accounts, align the script or code constants accordingly.

- **File write permissions**  
  If CSV/JSON writes fail, ensure you have write permissions in this repository directory (e.g., `chmod -R u+rwX .`).

---

## 7. Repository Structure (High‑Level)

```bash
cmd/                 # CLI entry points (subcommands)
recorded_stats/      # Example output directory (if enabled by code)
scripts/             # Helper scripts (e.g., bank_send_step.py)
*.go                 # Benchmark sources (e.g., main.go, chainMetrics.go, ibe_benchmark.go)
```

---

## 8. Notes

- All commands assume a local single‑node devnet at `127.0.0.1`. If using a remote host or non‑default ports, update constants or flags accordingly.
- For reproducibility, keep machine load stable during runs.
- Restart the devnet **after** each benchmark type to avoid cross‑contamination of state.

---

<!-- ## 9. License

This project is distributed under the terms specified in `LICENSE`. -->