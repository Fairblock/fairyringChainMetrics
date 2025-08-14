package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ───────────────────────── CONFIG (constants only) ─────────────────────────
const (
	fairyringd = "fairyringd"
	chainID    = "fairyring_devnet"
	homeDir    = "/home/grider644/go/src/github.com/FairBlock/fairyring/devnet_data/fairyring_devnet"
	keyring    = "test" // os|file|kwallet|pass|test|memory
	denom      = "ufairy"
	gasPrice   = "0.025" // numeric part only
	nodeRPC    = "tcp://127.0.0.1:26657"

	// actors
	funderName    = "wallet1"
	accountPrefix = "pepacc" // created accounts: pepacc0001..pepacc1000

	// scale
	numAccounts      = 1000
	signedTxsPerAcct = 10                // 1+2+3+4=10 per account across the 4 rounds
	fundInitialEach  = int64(50_000_000) // initial seeding per account
	fundRoundEach    = int64(5_000_000_000)

	// gas for plaintext (offline signed bank-send used only for encryption step)
	gasOfflineSend = int64(120_000)

	pepAutoAdjust      = 2.8              // initial --gas auto adjustment
	pepRetryBumpFactor = 1.30             // if OOG, retry with used*1.30
	pepRetryGasFloor   = int64(100000000) // minimum fixed gas on retry

	// ── NEW: multi-msg funding gas heuristics + batching size
	baseGasFunding   = int64(300_000)
	gasPerMsgFunding = int64(90_000)
	fundBatchSize    = 200 // msgs per funding tx (adjust as needed)

	// timings
	pollInterval       = 500 * time.Millisecond
	txInclusionTimeout = 180 * time.Second

	// dirs
	outRoot    = "./pep_out"
	signedRoot = "./pep_out/signed" // per-account subdirs
	logEveryN  = 50

	// target block offsets per round
	offsetRound1 = int64(100)
	offsetRound2 = int64(200)
	offsetRound3 = int64(300)
	offsetRound4 = int64(400)

	// concurrency for account workers (encryption/submission). Funding batches run sequentially.
	parAccounts = 100
)

// computed flags
var (
	txCommon   = []string{"--home", homeDir, "--keyring-backend", keyring, "--chain-id", chainID}
	keysCommon = []string{"--home", homeDir, "--keyring-backend", keyring}
	qCommon    = []string{"--node", nodeRPC}
)

// ─────────────────────────── helpers: exec ───────────────────────────

func run(ctx context.Context, cmd string, args ...string) (string, error) {
	c := exec.CommandContext(ctx, cmd, args...)
	var out bytes.Buffer
	c.Stdout = &out
	c.Stderr = &out
	err := c.Run()
	return out.String(), err
}

func runJSON(ctx context.Context, cmd string, args ...string) (map[string]any, error) {
	out, err := run(ctx, cmd, append(args, "--output", "json")...)
	if err != nil {
		return nil, fmt.Errorf("cmd failed: %s %s\n--- output ---\n%s", cmd, strings.Join(args, " "), out)
	}
	first := strings.Index(out, "{")
	last := strings.LastIndex(out, "}")
	if first == -1 || last == -1 || last <= first {
		return nil, fmt.Errorf("non-JSON output from %s %s\n--- output ---\n%s", cmd, strings.Join(args, " "), out)
	}
	out = out[first : last+1]
	var m map[string]any
	if err := json.Unmarshal([]byte(out), &m); err != nil {
		return nil, fmt.Errorf("json parse error from %s %s\n--- json ---\n%s\nerr=%v", cmd, strings.Join(args, " "), out, err)
	}
	return m, nil
}

// ─────────────────────────── chain queries ───────────────────────────

func status(ctx context.Context) (map[string]any, error) {
	return runJSON(ctx, fairyringd, append([]string{"status"}, qCommon...)...)
}

func latestHeightAndTime(ctx context.Context) (int64, time.Time, error) {
	s, err := status(ctx)
	if err != nil {
		return 0, time.Time{}, err
	}
	sync := map[string]any{}
	if v, ok := s["SyncInfo"].(map[string]any); ok {
		sync = v
	} else if v, ok := s["sync_info"].(map[string]any); ok {
		sync = v
	}
	hStr, _ := sync["latest_block_height"].(string)
	tStr, _ := sync["latest_block_time"].(string)
	h, _ := strconv.ParseInt(hStr, 10, 64)
	t, err := parseRFC3339Nanos(tStr)
	return h, t, err
}

func parseRFC3339Nanos(s string) (time.Time, error) {
	ss := strings.TrimSpace(s)
	if ss == "" {
		return time.Time{}, fmt.Errorf("empty time string")
	}
	hasZ := strings.HasSuffix(ss, "Z")
	ss = strings.TrimSuffix(ss, "Z")
	if strings.Contains(ss, ".") {
		parts := strings.SplitN(ss, ".", 2)
		frac := parts[1]
		if len(frac) < 6 {
			frac = (frac + "000000")[:6]
		} else {
			frac = frac[:6]
		}
		ss = parts[0] + "." + frac
	} else {
		ss = ss + ".000000"
	}
	if hasZ {
		ss = ss + "Z"
	}
	t, err := time.Parse(time.RFC3339Nano, ss)
	if err != nil {
		t, err2 := time.Parse("2006-01-02T15:04:05.000000Z07:00", ss)
		if err2 != nil {
			return time.Time{}, err
		}
		return t.UTC(), nil
	}
	return t.UTC(), nil
}

type blockHeader struct {
	Header struct {
		Height string `json:"height"`
		Time   string `json:"time"`
	} `json:"header"`
}

func queryBlockHeader(ctx context.Context, height int64) (time.Time, error) {
	m, err := runJSON(ctx, fairyringd, append([]string{"q", "block", "--type=height", strconv.FormatInt(height, 10)}, qCommon...)...)
	if err != nil {
		return time.Time{}, err
	}
	b, _ := json.Marshal(m)
	var bh blockHeader
	if err := json.Unmarshal(b, &bh); err != nil {
		return time.Time{}, fmt.Errorf("bad block JSON shape: %v", err)
	}
	return parseRFC3339Nanos(bh.Header.Time)
}

func blockTimeForHeight(ctx context.Context, height int64) (float64, error) {
	if height <= 1 {
		return 0, errors.New("invalid height")
	}
	t, err := queryBlockHeader(ctx, height)
	if err != nil {
		return 0, err
	}
	tp, err := queryBlockHeader(ctx, height-1)
	if err != nil {
		return 0, err
	}
	return t.Sub(tp).Seconds(), nil
}

func waitNextBlock(ctx context.Context, after int64) (int64, time.Time, error) {
	for {
		h, t, err := latestHeightAndTime(ctx)
		if err == nil && h > after {
			return h, t, nil
		}
		select {
		case <-ctx.Done():
			return 0, time.Time{}, ctx.Err()
		case <-time.After(pollInterval):
		}
	}
}

func waitUntilHeight(ctx context.Context, target int64) (int64, error) {
	for {
		h, _, err := latestHeightAndTime(ctx)
		if err == nil && h >= target {
			return h, nil
		}
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-time.After(pollInterval):
		}
	}
}

// ───────────────────────── keys / accounts ─────────────────────────

func keysShowAddress(ctx context.Context, name string) (string, error) {
	out, err := run(ctx, fairyringd, append([]string{"keys", "show", name}, append(keysCommon, "--address")...)...)
	if err != nil {
		return "", fmt.Errorf("keys show %s: %v\n%s", name, err, out)
	}
	return strings.TrimSpace(out), nil
}

func ensureKey(ctx context.Context, name string) (string, error) {
	addr, err := keysShowAddress(ctx, name)
	if err == nil && addr != "" {
		return addr, nil
	}
	_, err = run(ctx, fairyringd, append([]string{"keys", "add", name}, append(keysCommon, "--algo", "secp256k1")...)...)
	if err != nil {
		return "", fmt.Errorf("keys add %s failed: %v", name, err)
	}
	return keysShowAddress(ctx, name)
}

type baseAccount struct {
	Account struct {
		BaseAccount struct {
			Address       string `json:"address"`
			AccountNumber string `json:"account_number"`
			Sequence      string `json:"sequence"`
		} `json:"base_account"`
	} `json:"account"`
}

func getAccountNumbers(ctx context.Context, addr string) (acctNum, seq uint64, err error) {
	m, err := runJSON(ctx, fairyringd, append([]string{"q", "auth", "account", addr}, qCommon...)...)
	if err != nil {
		return 0, 0, err
	}
	b, _ := json.Marshal(m)
	var ba baseAccount
	if err := json.Unmarshal(b, &ba); err != nil {
		return 0, 0, err
	}
	an, _ := strconv.ParseUint(ba.Account.BaseAccount.AccountNumber, 10, 64)
	sq, _ := strconv.ParseUint(ba.Account.BaseAccount.Sequence, 10, 64)
	return an, sq, nil
}

// ───────────────────────── funding (ONLINE, BATCHED MULTI-MSG) ─────────────────────────

type coin struct {
	Denom  string `json:"denom"`
	Amount string `json:"amount"`
}
type msgSend struct {
	Type        string `json:"@type"`
	FromAddress string `json:"from_address"`
	ToAddress   string `json:"to_address"`
	Amount      []coin `json:"amount"`
}
type txFile struct {
	Body struct {
		Messages                    []any  `json:"messages"`
		Memo                        string `json:"memo"`
		TimeoutHeight               string `json:"timeout_height"`
		ExtensionOptions            []any  `json:"extension_options"`
		NonCriticalExtensionOptions []any  `json:"non_critical_extension_options"`
	} `json:"body"`
	AuthInfo struct {
		SignerInfos []any `json:"signer_infos"`
		Fee         struct {
			Amount   []coin `json:"amount"`
			GasLimit string `json:"gas_limit"`
			Payer    string `json:"payer"`
			Granter  string `json:"granter"`
		} `json:"fee"`
	} `json:"auth_info"`
	Signatures []string `json:"signatures"`
}

func ceilFee(gas int64) string {
	gf := float64(gas)
	gpf, _ := strconv.ParseFloat(gasPrice, 64)
	fee := int64(gf*gpf + 0.999999) // ceil-ish
	return strconv.FormatInt(fee, 10)
}

func writeJSON(path string, v any) error {
	b, _ := json.MarshalIndent(v, "", "  ")
	return os.WriteFile(path, b, 0o644)
}

func buildUnsignedSingleSend(fromAddr, toAddr string, amt int64, memo string, gas int64) *txFile {
	var tx txFile
	tx.Body.Messages = []any{
		msgSend{
			Type:        "/cosmos.bank.v1beta1.MsgSend",
			FromAddress: fromAddr,
			ToAddress:   toAddr,
			Amount:      []coin{{Denom: denom, Amount: strconv.FormatInt(amt, 10)}},
		},
	}
	tx.Body.Memo = memo
	tx.Body.TimeoutHeight = "0"
	tx.Body.ExtensionOptions = []any{}
	tx.Body.NonCriticalExtensionOptions = []any{}

	tx.AuthInfo.Fee.GasLimit = strconv.FormatInt(gas, 10)
	tx.AuthInfo.Fee.Amount = []coin{{Denom: denom, Amount: ceilFee(gas)}}
	return &tx
}

func buildFundingUnsignedTx(funderAddr string, recipients []string, amount int64, gasLimit int64, memo string) *txFile {
	var tx txFile
	msgs := make([]any, 0, len(recipients))
	for _, rcpt := range recipients {
		msgs = append(msgs, msgSend{
			Type:        "/cosmos.bank.v1beta1.MsgSend",
			FromAddress: funderAddr,
			ToAddress:   rcpt,
			Amount:      []coin{{Denom: denom, Amount: strconv.FormatInt(amount, 10)}},
		})
	}
	tx.Body.Messages = msgs
	tx.Body.Memo = memo
	tx.Body.TimeoutHeight = "0"
	tx.Body.ExtensionOptions = []any{}
	tx.Body.NonCriticalExtensionOptions = []any{}

	tx.AuthInfo.Fee.GasLimit = strconv.FormatInt(gasLimit, 10)
	tx.AuthInfo.Fee.Amount = []coin{{Denom: denom, Amount: ceilFee(gasLimit)}}
	return &tx
}

func signOnlineTxToFile(ctx context.Context, unsignedPath, signedPath, signerName string) error {
	args := []string{
		"tx", "sign", unsignedPath,
		"--from", signerName,
	}
	args = append(args, txCommon...)
	// online sign (no --offline), let CLI fetch account number/sequence
	args = append(args, "--output-document", signedPath)
	out, err := run(ctx, fairyringd, args...)
	if err != nil {
		return fmt.Errorf("online sign failed: %v\n%s", err, out)
	}
	return nil
}

func broadcastFileAndWait(ctx context.Context, signedPath string) (string, error) {
	m, err := runJSON(ctx, fairyringd, append([]string{"tx", "broadcast", signedPath}, qCommon...)...)
	if err != nil {
		return "", err
	}
	checkCode := int(jsonInt(m["code"]))
	txh := jsonStr(m["txhash"])
	if checkCode != 0 {
		return "", fmt.Errorf("broadcast rejected (CheckTx) code=%d log=%s", checkCode, jsonStr(m["raw_log"]))
	}
	included, err := waitTxInclusion(ctx, txh, txInclusionTimeout)
	if err != nil {
		return "", err
	}
	deliverCode := int(jsonInt(included["code"]))
	if deliverCode != 0 {
		return "", fmt.Errorf("broadcast failed (DeliverTx) code=%d log=%s", deliverCode, jsonStr(included["raw_log"]))
	}
	return txh, nil
}

// Sequential (to avoid sequence races on the funder); will retry a batch once with +30% gas if out-of-gas is detected.
func fundAccountsOnlineBatched(ctx context.Context, funderName, funderAddr string, recipients []string, amount int64, memo string, batchSize int) error {
	tmpDir := filepath.Join(outRoot, "tmp_funding")
	_ = os.MkdirAll(tmpDir, 0o755)

	for i := 0; i < len(recipients); i += batchSize {
		end := i + batchSize
		if end > len(recipients) {
			end = len(recipients)
		}
		batch := recipients[i:end]
		gl := baseGasFunding + gasPerMsgFunding*int64(len(batch))

		tryOnce := func(glimit int64) (string, error) {
			unsigned := filepath.Join(tmpDir, fmt.Sprintf("fund_unsigned_%06d.json", i))
			signed := filepath.Join(tmpDir, fmt.Sprintf("fund_signed_%06d.json", i))
			tx := buildFundingUnsignedTx(funderAddr, batch, amount, glimit, memo)
			if err := writeJSON(unsigned, tx); err != nil {
				return "", err
			}
			if err := signOnlineTxToFile(ctx, unsigned, signed, funderName); err != nil {
				_ = os.Remove(unsigned)
				return "", err
			}
			_ = os.Remove(unsigned)
			txh, err := broadcastFileAndWait(ctx, signed)
			_ = os.Remove(signed)
			return txh, err
		}

		txh, err := tryOnce(gl)
		if err != nil && strings.Contains(strings.ToLower(err.Error()), "out of gas") {
			// bump and retry once
			gl = int64(float64(gl) * 1.3)
			txh, err = tryOnce(gl)
		}
		if err != nil {
			return fmt.Errorf("fund batch %d-%d failed: %w", i, end-1, err)
		}
		if ((i/batchSize)+1)%5 == 0 || end == len(recipients) {
			fmt.Printf("  funded batch %d/%d (tx=%s)\n", (i/batchSize)+1, (len(recipients)+batchSize-1)/batchSize, txh)
		}
	}
	return nil
}

// ───────────────────────── inclusion wait ─────────────────────────

func waitTxInclusion(ctx context.Context, txhash string, timeout time.Duration) (map[string]any, error) {
	dead := time.Now().Add(timeout)
	for time.Now().Before(dead) {
		m, err := runJSON(ctx, fairyringd, append([]string{"q", "tx", txhash}, qCommon...)...)
		if err == nil {
			if txr, ok := m["tx_response"].(map[string]any); ok {
				m = txr
			}
			h := jsonStr(m["height"])
			code := int(jsonInt(m["code"]))
			if h != "" || code != 0 {
				return m, nil
			}
		}
		time.Sleep(pollInterval)
	}
	return nil, fmt.Errorf("timeout waiting inclusion %s", txhash)
}

// ───────────────────────── plaintext signing (offline) ─────────────────────────

func signOfflineTx(ctx context.Context, unsignedPath, signedPath, signerName string, acctNum uint64, seq uint64) error {
	args := []string{
		"tx", "sign", unsignedPath,
		"--from", signerName,
		"--account-number", strconv.FormatUint(acctNum, 10),
		"--sequence", strconv.FormatUint(seq, 10),
		"--offline",
		"--sign-mode", "direct",
	}
	args = append(args, txCommon...)
	args = append(args, "--output-document", signedPath)
	out, err := run(ctx, fairyringd, args...)
	if err != nil {
		return fmt.Errorf("offline sign failed (acct=%s an=%d seq=%d): %v\n%s", signerName, acctNum, seq, err, out)
	}
	return nil
}

// ───────────────────────── PEP helpers ─────────────────────────

type pepActive struct {
	Active struct {
		PublicKey string `json:"public_key"`
	} `json:"active_pubkey"`
}

// extract "gasWanted: N, gasUsed: M" from a raw_log string
func parseGasFromRawLog(raw string) (wanted, used int64, ok bool) {
	findNum := func(after string) (int64, bool) {
		i := strings.Index(raw, after)
		if i < 0 {
			return 0, false
		}
		j := i + len(after)
		// read digits
		k := j
		for k < len(raw) && raw[k] >= '0' && raw[k] <= '9' {
			k++
		}
		if k == j {
			return 0, false
		}
		n, _ := strconv.ParseInt(raw[j:k], 10, 64)
		return n, true
	}
	w, okW := findNum("gasWanted: ")
	u, okU := findNum("gasUsed: ")
	return w, u, okW && okU
}

// run submit-encrypted-tx with either auto gas (adjust) or fixed gas
func submitPepOnce(ctx context.Context, fromName, data string, target int64, memo string, useAuto bool, gasLimit int64, gasAdjust float64) (string, map[string]any, error) {
	args := []string{
		"tx", "pep", "submit-encrypted-tx", data, strconv.FormatInt(target, 10),
		"--from", fromName,
	}
	args = append(args, txCommon...)
	args = append(args, qCommon...)
	if useAuto {
		args = append(args, "--gas", "auto", "--gas-adjustment", fmt.Sprintf("%.2f", gasAdjust))
	} else {
		args = append(args, "--gas", strconv.FormatInt(gasLimit, 10))
	}
	args = append(args,
		"--gas-prices", gasPrice+denom,
		"--broadcast-mode=sync", // CheckTx
		"--note", memo,
		"-y",
	)

	m, err := runJSON(ctx, fairyringd, args...)
	if err != nil {
		return "", nil, fmt.Errorf("submit-encrypted-tx cmd failed: %w", err)
	}
	checkCode := int(jsonInt(m["code"]))
	txh := jsonStr(m["txhash"])
	if checkCode != 0 {
		return "", m, fmt.Errorf("submit-encrypted-tx rejected (CheckTx) code=%d log=%s", checkCode, jsonStr(m["raw_log"]))
	}

	// Wait DeliverTx
	included, err := waitTxInclusion(ctx, txh, txInclusionTimeout)
	if err != nil {
		return txh, included, fmt.Errorf("submit-encrypted-tx inclusion wait: %w", err)
	}
	deliverCode := int(jsonInt(included["code"]))
	if deliverCode != 0 {
		return txh, included, fmt.Errorf("submit-encrypted-tx failed (DeliverTx) code=%d log=%s", deliverCode, jsonStr(included["raw_log"]))
	}
	return txh, included, nil
}

// public robust wrapper: auto first, if OOG then retry once with fixed gas
func pepSubmitEncryptedTxRobust(ctx context.Context, fromName, data string, target int64, memo string) (string, error) {
	// 1) First attempt: auto with generous adjustment
	txh, included, err := submitPepOnce(ctx, fromName, data, target, memo, true, 0, pepAutoAdjust)
	if err == nil {
		return txh, nil
	}

	// If not OOG, just return the error
	if !strings.Contains(err.Error(), "out of gas") {
		return "", err
	}

	// Try to parse gasUsed from the DeliverTx log to set a fixed limit
	var rawLog string
	if included != nil {
		rawLog = jsonStr(included["raw_log"])
	}
	_, used, ok := parseGasFromRawLog(rawLog)
	var retryGas int64
	if ok && used > 0 {
		retryGas = int64(float64(used)*pepRetryBumpFactor + 0.5)
	} else {
		// didn't parse; fall back to a big fixed cap (previous wanted * factor is unknown)
		// use a conservative multi-million gas ceiling
		retryGas = 5_000_000
	}
	if retryGas < pepRetryGasFloor {
		retryGas = pepRetryGasFloor
	}

	// 2) Retry with fixed gas
	txh2, _, err2 := submitPepOnce(ctx, fromName, data, target, memo, false, retryGas, 0)
	if err2 == nil {
		return txh2, nil
	}
	return "", fmt.Errorf("retry (fixed gas=%d) failed: %w", retryGas, err2)
}

func getPepActivePubKey(ctx context.Context) (string, error) {
	m, err := runJSON(ctx, fairyringd, "q", "pep", "show-active-pub-key")
	if err != nil {
		return "", err
	}
	b, _ := json.Marshal(m)
	var pa pepActive
	if err := json.Unmarshal(b, &pa); err != nil {
		return "", err
	}
	if pa.Active.PublicKey == "" {
		return "", errors.New("active pep pubkey empty")
	}
	return pa.Active.PublicKey, nil
}

func encryptPlaintext(ctx context.Context, target int64, pubkey string, plaintext string) (string, error) {
	args := []string{"encrypt", strconv.FormatInt(target, 10), pubkey, plaintext}
	out, err := run(ctx, fairyringd, args...)
	if err != nil {
		return "", fmt.Errorf("encrypt failed: %v\n%s", err, out)
	}
	return strings.TrimSpace(out), nil
}

func pepSubmitEncryptedTx(ctx context.Context, fromName, data string, target int64, memo string) (string, error) {
	args := []string{
		"tx", "pep", "submit-encrypted-tx", data, strconv.FormatInt(target, 10),
		"--from", fromName,
	}
	args = append(args, txCommon...)
	args = append(args, qCommon...)
	args = append(args,
		"--gas", "auto",
		"--gas-adjustment", "1.7",
		"--gas-prices", gasPrice+denom,
		"--broadcast-mode=sync",
		"--note", memo,
		"-y",
	)

	m, err := runJSON(ctx, fairyringd, args...)
	if err != nil {
		return "", fmt.Errorf("submit-encrypted-tx cmd failed: %w", err)
	}
	checkCode := int(jsonInt(m["code"]))
	txh := jsonStr(m["txhash"])
	if checkCode != 0 {
		return "", fmt.Errorf("submit-encrypted-tx rejected (CheckTx) code=%d log=%s", checkCode, jsonStr(m["raw_log"]))
	}

	included, err := waitTxInclusion(ctx, txh, txInclusionTimeout)
	if err != nil {
		return "", fmt.Errorf("submit-encrypted-tx inclusion wait: %w", err)
	}
	deliverCode := int(jsonInt(included["code"]))
	if deliverCode != 0 {
		return "", fmt.Errorf("submit-encrypted-tx failed (DeliverTx) code=%d log=%s", deliverCode, jsonStr(included["raw_log"]))
	}
	return txh, nil
}

// ───────────────────────── utils ─────────────────────────

func mustMkdirAll(p string) {
	if err := os.MkdirAll(p, 0o755); err != nil {
		panic(err)
	}
}

func jsonStr(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprintf("%v", v)
}
func jsonInt(v any) int64 {
	switch t := v.(type) {
	case float64:
		return int64(t)
	case string:
		i, _ := strconv.ParseInt(t, 10, 64)
		return i
	default:
		return 0
	}
}

// sequences used per round without reuse:
// round=1 -> [1]
// round=2 -> [2,3]
// round=3 -> [4,5,6]
// round=4 -> [7,8,9,10]
func sequencesForRound(round int) []int {
	start := 1 + (round-1)*round/2
	out := make([]int, round)
	for i := 0; i < round; i++ {
		out[i] = start + i
	}
	return out
}

// ───────────────────────── main benchmark ─────────────────────────

func IBEBenchmark() {
	ctx := context.Background()
	mustMkdirAll(outRoot)
	mustMkdirAll(signedRoot)

	// 1) baseline avg block time
	fmt.Println("measuring baseline over 10 blocks…")
	h0, _, err := latestHeightAndTime(ctx)
	if err != nil {
		panic(err)
	}
	var times []float64
	prevH, prevT, _ := waitNextBlock(ctx, h0)
	for i := 0; i < 10; i++ {
		h, t, _ := waitNextBlock(ctx, prevH)
		dt := t.Sub(prevT).Seconds()
		times = append(times, dt)
		prevH, prevT = h, t
		fmt.Printf("  baseline block %d: %.3fs\n", h, dt)
	}
	var sum float64
	for _, x := range times {
		sum += x
	}
	baseline := sum / float64(len(times))
	fmt.Printf("baseline average block time: %.3fs\n", baseline)

	// Ensure funder exists
	funderAddr, err := ensureKey(ctx, funderName)
	if err != nil {
		panic(err)
	}
	_ = funderAddr

	// 2) create N new accounts (just keys)
	fmt.Printf("creating %d accounts…\n", numAccounts)
	names := make([]string, numAccounts)
	addrs := make([]string, numAccounts)
	wg := sync.WaitGroup{}
	sem := make(chan struct{}, parAccounts)
	errs := make(chan error, numAccounts)
	for i := 0; i < numAccounts; i++ {
		i := i
		wg.Add(1)
		sem <- struct{}{}
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			name := fmt.Sprintf("%s%04d", accountPrefix, i+1)
			addr, e := ensureKey(ctx, name)
			if e != nil {
				errs <- fmt.Errorf("ensureKey(%s): %w", name, e)
				return
			}
			names[i] = name
			addrs[i] = addr
			if (i+1)%logEveryN == 0 {
				fmt.Printf("  created %d/%d\n", i+1, numAccounts)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		if e != nil {
			panic(e)
		}
	}
	fmt.Printf("accounts created. example: %s => %s\n", names[0], addrs[0])

	// Initial funding: batched multi-msg, online sign
	fmt.Printf("initial funding %d accounts with %d%s each (batched multi-msg)…\n", numAccounts, fundInitialEach, denom)
	if err := fundAccountsOnlineBatched(ctx, funderName, funderAddr, addrs, fundInitialEach, "initial funding (batched)", fundBatchSize); err != nil {
		panic(err)
	}
	fmt.Println("initial funding complete ✔")

	// 4) pep_nonce per account (start at 1)
	pepNonce := make([]uint64, numAccounts)
	for i := range pepNonce {
		pepNonce[i] = 1
	}

	// 5) for each account, offline sign 10 bank send txs using pep_nonce for sequence
	fmt.Printf("offline signing %d txs per account (pep_nonce as sequence)…\n", signedTxsPerAcct)
	mustMkdirAll(signedRoot)
	signErrs := make(chan error, numAccounts)
	wg = sync.WaitGroup{}
	sem = make(chan struct{}, parAccounts)
	for i := 0; i < numAccounts; i++ {
		i := i
		wg.Add(1)
		sem <- struct{}{}
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			name := names[i]
			addr := addrs[i]
			acctNum, _, err := getAccountNumbers(ctx, addr)
			if err != nil {
				signErrs <- fmt.Errorf("getAccountNumbers(%s/%s): %w", name, addr, err)
				return
			}
			acctDir := filepath.Join(signedRoot, addr)
			_ = os.MkdirAll(acctDir, 0o755)

			for j := 0; j < signedTxsPerAcct; j++ {
				seq := pepNonce[i]
				msg := buildUnsignedSingleSend(addr, funderAddr, 1, "", gasOfflineSend)
				unsigned := filepath.Join(acctDir, fmt.Sprintf("unsigned_seq_%06d.json", seq))
				signed := filepath.Join(acctDir, fmt.Sprintf("signed_seq_%06d.json", seq))
				if err := writeJSON(unsigned, msg); err != nil {
					signErrs <- fmt.Errorf("write unsigned: %w", err)
					return
				}
				if err := signOfflineTx(ctx, unsigned, signed, name, acctNum, seq); err != nil {
					signErrs <- err
					return
				}
				_ = os.Remove(unsigned)
				pepNonce[i]++
			}
			if (i+1)%logEveryN == 0 {
				fmt.Printf("  offline signed: %d/%d accounts\n", i+1, numAccounts)
			}
		}()
	}
	wg.Wait()
	close(signErrs)
	for e := range signErrs {
		if e != nil {
			panic(e)
		}
	}
	fmt.Println("offline signing complete ✔")

	// 5b) fetch active PEP pubkey
	pubkey, err := getPepActivePubKey(ctx)
	if err != nil {
		panic(err)
	}
	fmt.Printf("pep active pubkey: %s\n", pubkey)

	// ROUNDS: 1, 2, 3, 4 txs per account, with target offsets 100/200/300/400
	rounds := []struct {
		rnum   int
		offset int64
	}{
		{1, offsetRound1},
		{2, offsetRound2},
		{3, offsetRound3},
		{4, offsetRound4},
	}

	for _, r := range rounds {
		// 6) current height and target
		curH, _, err := latestHeightAndTime(ctx)
		if err != nil {
			panic(err)
		}
		targetH := curH + r.offset
		fmt.Printf("[round %d] current height=%d -> target height=%d\n", r.rnum, curH, targetH)

		// sequences to use this round (non-overlapping)
		seqs := sequencesForRound(r.rnum)

		// 7) encrypt signed txs (per-account sequential; across accounts parallel)
		fmt.Printf("[round %d] encrypting %d signed tx(s) per account for target %d…\n", r.rnum, r.rnum, targetH)
		type encItem struct {
			Name string
			Addr string
			Data []string
		}
		encrypted := make([]encItem, numAccounts)
		encErrs := make(chan error, numAccounts)
		wg = sync.WaitGroup{}
		sem = make(chan struct{}, parAccounts)
		for i := 0; i < numAccounts; i++ {
			i := i
			wg.Add(1)
			sem <- struct{}{}
			go func() {
				defer wg.Done()
				defer func() { <-sem }()
				addr := addrs[i]
				name := names[i]
				acctDir := filepath.Join(signedRoot, addr)

				var cts []string
				for _, seq := range seqs {
					signedPath := filepath.Join(acctDir, fmt.Sprintf("signed_seq_%06d.json", seq))
					bz, err := os.ReadFile(signedPath)
					if err != nil {
						encErrs <- fmt.Errorf("[round %d] read signed (%s/%s): %w", r.rnum, name, signedPath, err)
						return
					}
					ct, err := encryptPlaintext(ctx, targetH, pubkey, string(bz))
					if err != nil {
						encErrs <- fmt.Errorf("[round %d] encrypt(%s seq=%d): %w", r.rnum, name, seq, err)
						return
					}
					cts = append(cts, ct)
					_ = os.Remove(signedPath)
				}
				encrypted[i] = encItem{Name: name, Addr: addr, Data: cts}
				if (i+1)%logEveryN == 0 {
					fmt.Printf("  [round %d] encrypted %d/%d accounts\n", r.rnum, i+1, numAccounts)
				}
			}()
		}
		wg.Wait()
		close(encErrs)
		for e := range encErrs {
			if e != nil {
				panic(e)
			}
		}
		fmt.Printf("[round %d] encryption complete ✔\n", r.rnum)

		// 8) submit N encrypted txs per account (per-account sequential), wait inclusion
		fmt.Printf("[round %d] submitting %d encrypted tx(s) per account…\n", r.rnum, r.rnum)
		subErrs := make(chan error, numAccounts)
		wg = sync.WaitGroup{}
		sem = make(chan struct{}, parAccounts)
		for i := 0; i < numAccounts; i++ {
			i := i
			wg.Add(1)
			sem <- struct{}{}
			go func() {
				defer wg.Done()
				defer func() { <-sem }()
				item := encrypted[i]
				if len(item.Data) != r.rnum {
					subErrs <- fmt.Errorf("[round %d] missing ciphertexts for %s", r.rnum, item.Name)
					return
				}
				for j, ct := range item.Data {
					memo := fmt.Sprintf("pep round=%d item=%d", r.rnum, j+1)
					if _, err := pepSubmitEncryptedTxRobust(ctx, item.Name, ct, targetH, memo); err != nil {
						subErrs <- fmt.Errorf("[round %d] submit(%s) item=%d: %w", r.rnum, item.Name, j+1, err)
						return
					}
				}
				if (i+1)%logEveryN == 0 {
					fmt.Printf("  [round %d] submitted & included %d/%d\n", r.rnum, i+1, numAccounts)
				}
			}()
		}
		wg.Wait()
		close(subErrs)
		var hadErr bool
		for e := range subErrs {
			if e != nil {
				hadErr = true
				fmt.Println("ERROR:", e.Error())
			}
		}
		if hadErr {
			fmt.Printf("[round %d] WARNING: some accounts failed to submit; proceeding to refuel and timing.\n", r.rnum)
		} else {
			fmt.Printf("[round %d] all submit-encrypted-tx included ✔\n", r.rnum)
		}

		// Refuel with fixed amount using batched multi-msg funding
		fmt.Printf("[round %d] refueling %d accounts with %d%s each (batched multi-msg)…\n", r.rnum, numAccounts, fundRoundEach, denom)
		if err := fundAccountsOnlineBatched(ctx, funderName, funderAddr, addrs, fundRoundEach, fmt.Sprintf("refuel round=%d (batched)", r.rnum), fundBatchSize); err != nil {
			panic(err)
		}
		fmt.Printf("[round %d] refuel complete ✔\n", r.rnum)

		// 9) wait to target height and measure block time (target and next)
		fmt.Printf("[round %d] waiting for target height %d…\n", r.rnum, targetH)
		if _, err := waitUntilHeight(ctx, targetH); err != nil {
			panic(err)
		}
		bt, err := blockTimeForHeight(ctx, targetH)
		if err != nil {
			panic(err)
		}

		fmt.Printf("[round %d] waiting for height %d…\n", r.rnum, targetH+1)
		if _, err := waitUntilHeight(ctx, targetH+1); err != nil {
			panic(err)
		}
		btn, err := blockTimeForHeight(ctx, targetH+1)
		if err != nil {
			panic(err)
		}

		fmt.Printf("[round %d] waiting for height %d…\n", r.rnum, targetH+2)
		if _, err := waitUntilHeight(ctx, targetH+2); err != nil {
			panic(err)
		}
		btn2, err := blockTimeForHeight(ctx, targetH+2)
		if err != nil {
			panic(err)
		}

		fmt.Printf("[round %d] target block %d time: %.3fs (baseline %.3fs, Δ=%.3fs)\n",
			r.rnum, targetH, bt, baseline, bt-baseline)

		fmt.Printf("[round %d] processed block %d time: %.3fs (baseline %.3fs, Δ=%.3fs)\n",
			r.rnum, targetH+1, btn, baseline, btn-baseline)

		fmt.Printf("[round %d] processed block %d time: %.3fs (baseline %.3fs, Δ=%.3fs)\n",
			r.rnum, targetH+2, btn2, baseline, btn2-baseline)
	}

	// Optional: cleanup any leftover signed files
	_ = filepath.WalkDir(signedRoot, func(path string, d fs.DirEntry, _ error) error {
		if d == nil {
			return nil
		}
		if !d.IsDir() && strings.HasSuffix(d.Name(), ".json") {
			_ = os.Remove(path)
		}
		return nil
	})
}
