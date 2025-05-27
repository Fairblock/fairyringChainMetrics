package main

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"log"
	"net"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	baseURL     = "https://testnet-rpc.fairblock.network"
	workerCount = 1000
	retries     = 5
	barWidth    = 50

	// ANSI colors
	colorGreen = "\x1b[32m"
	colorGrey  = "\x1b[90m"
	colorReset = "\x1b[0m"
)

var spinnerFrames = []string{"⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"}

// in-process DNS cache
var (
	dnsCache = make(map[string][]net.IP)
	dnsMu    sync.RWMutex
)

func lookupHost(ctx context.Context, host string) ([]net.IP, error) {
	dnsMu.RLock()
	if ips, ok := dnsCache[host]; ok {
		dnsMu.RUnlock()
		return ips, nil
	}
	dnsMu.RUnlock()

	ips, err := net.DefaultResolver.LookupIP(ctx, "ip", host)
	if err != nil {
		return nil, err
	}
	dnsMu.Lock()
	dnsCache[host] = ips
	dnsMu.Unlock()
	return ips, nil
}

func customDialContext(ctx context.Context, network, addr string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, err
	}
	ips, err := lookupHost(ctx, host)
	if err != nil {
		return nil, err
	}
	var lastErr error
	for _, ip := range ips {
		dialer := &net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}
		conn, err := dialer.DialContext(ctx, network, net.JoinHostPort(ip.String(), port))
		if err == nil {
			return conn, nil
		}
		lastErr = err
	}
	return nil, lastErr
}

var client = &http.Client{
	Transport: &http.Transport{
		DialContext:         customDialContext,
		MaxIdleConns:        1000,
		MaxIdleConnsPerHost: 1000,
	},
	Timeout: 30 * time.Second,
}

// globals for user metrics
var (
	allUsers          = make(map[string]struct{})
	userFirstSeenWeek = make(map[string]int)
	// cumulativeTotalTxs int
	// skipped            int
)

// RPC response structs
type StatusResponse struct {
	Result struct {
		SyncInfo struct {
			LatestBlockHeight string `json:"latest_block_height"`
		} `json:"sync_info"`
	} `json:"result"`
}

type BlockResponse struct {
	Result struct {
		Block struct {
			Header struct {
				Time string `json:"time"`
			} `json:"header"`
			LastCommit struct {
				Precommits []*struct {
					ValidatorAddress string `json:"validator_address"`
				} `json:"precommits"`
			} `json:"last_commit"`
		} `json:"block"`
	} `json:"result"`
}

type BlockResultsResponse struct {
	Result struct {
		TxsResults []struct {
			Events []struct {
				Type       string `json:"type"`
				Attributes []struct {
					Key, Value string
				} `json:"attributes"`
			} `json:"events"`
		} `json:"txs_results"`
	} `json:"result"`
}

// BlockResult holds data from one block
type BlockResult struct {
	Height                         int
	Timestamp                      time.Time
	NewEncryptedTxSubmitted        int
	RevertedEncryptedTx            int
	NewGeneralEncryptedTxSubmitted int
	KeyshareSent                   int
	KeyshareAggregated             int
	GeneralKeyshareAggregated      int
	ValidatorVotes                 map[string]int
	TxCountPerSender               map[string]int
	TotalTxs                       int
}

// WeekData aggregates per-week
type WeekData struct {
	Timestamps                     []time.Time
	NewEncryptedTxSubmitted        int
	RevertedEncryptedTx            int
	NewGeneralEncryptedTxSubmitted int
	KeyshareSent                   int
	KeyshareAggregated             int
	GeneralKeyshareAggregated      int
	ValidatorVotes                 map[string]int

	ActiveUsers    map[string]struct{}
	NewUsers       map[string]struct{}
	TxCountPerUser map[string]int
	WeeklyTotalTxs int
}

// getWithRetry retries GET up to retries times
func getWithRetry(url string) ([]byte, error) {
	var lastErr error
	for i := 1; i <= retries; i++ {
		resp, err := client.Get(url)
		if err != nil {
			lastErr = err
			time.Sleep(time.Duration(i) * time.Second)
			continue
		}
		body, err := ioutil.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			lastErr = err
			time.Sleep(time.Duration(i) * time.Second)
			continue
		}
		return body, nil
	}
	return nil, fmt.Errorf("after %d retries, last error: %w", retries, lastErr)
}

func getCurrentBlockHeight() (int, error) {
	b, err := getWithRetry(baseURL + "/status")
	if err != nil {
		return 0, err
	}
	var s StatusResponse
	if err := json.Unmarshal(b, &s); err != nil {
		return 0, err
	}

	return strconv.Atoi(s.Result.SyncInfo.LatestBlockHeight)
}

func fetchBlock(h int) (*BlockResponse, error) {
	b, err := getWithRetry(fmt.Sprintf("%s/block?height=%d", baseURL, h))
	if err != nil {
		return nil, err
	}
	var br BlockResponse
	if err := json.Unmarshal(b, &br); err != nil {
		return nil, err
	}
	return &br, nil
}

func fetchBlockResults(h int) (*BlockResultsResponse, error) {
	b, err := getWithRetry(fmt.Sprintf("%s/block_results?height=%d", baseURL, h))
	if err != nil {
		return nil, err
	}
	var br BlockResultsResponse
	if err := json.Unmarshal(b, &br); err != nil {
		return nil, err
	}
	return &br, nil
}

func processBlock(height int, _ time.Time) (*BlockResult, error) {
	blk, err := fetchBlock(height)
	if err != nil {
		return nil, fmt.Errorf("fetch block %d: %v", height, err)
	}
	br, err := fetchBlockResults(height)
	if err != nil {
		return nil, fmt.Errorf("fetch block_results %d: %v", height, err)
	}
	ts, err := time.Parse(time.RFC3339Nano, blk.Result.Block.Header.Time)
	if err != nil {
		return nil, fmt.Errorf("parse time %d: %v", height, err)
	}

	res := &BlockResult{
		Height:           height,
		Timestamp:        ts,
		ValidatorVotes:   make(map[string]int),
		TxCountPerSender: make(map[string]int),
	}

	for _, tx := range br.Result.TxsResults {
		res.TotalTxs++
		senders := make(map[string]struct{})
		for _, ev := range tx.Events {
			switch ev.Type {
			case "new-encrypted-tx-submitted":
				res.NewEncryptedTxSubmitted++
			case "reverted-encrypted-tx":
				res.RevertedEncryptedTx++
			case "new-general-encrypted-tx-submitted":
				res.NewGeneralEncryptedTxSubmitted++
			case "keyshare-sent":
				res.KeyshareSent++
			case "keyshare-aggregated":
				res.KeyshareAggregated++
			case "general-keyshare-aggregated":
				res.GeneralKeyshareAggregated++
			}
			if ev.Type == "message" {
				for _, attr := range ev.Attributes {
					if attr.Key == "sender" {
						senders[attr.Value] = struct{}{}
					}
				}
			}
		}
		for s := range senders {
			res.TxCountPerSender[s]++
		}
	}
	for _, pc := range blk.Result.Block.LastCommit.Precommits {
		if pc != nil && pc.ValidatorAddress != "" {
			res.ValidatorVotes[pc.ValidatorAddress]++
		}
	}
	return res, nil
}

func printProgress(done, total int, elapsed time.Duration, spinner string) {
	pct := float64(done) / float64(total) * 100
	filled := int((pct / 100) * barWidth)
	if filled > barWidth {
		filled = barWidth
	}
	empty := barWidth - filled

	eta := ""
	if done > 0 {
		rate := float64(done) / elapsed.Seconds()
		rem := float64(total-done) / rate
		d := time.Duration(rem) * time.Second
		h, m, s := int(d.Hours()), int(d.Minutes())%60, int(d.Seconds())%60
		eta = fmt.Sprintf("%02d:%02d:%02d", h, m, s)
	}

	bar := colorGreen + strings.Repeat("=", filled) + colorReset +
		colorGrey + strings.Repeat(" ", empty) + colorReset

	if eta != "" {
		fmt.Printf("\r%s %d/%d  %6.1f%%  [%s]  ETA %s",
			spinner, done, total, pct, bar, eta)
	} else {
		fmt.Printf("\r%s %d/%d  %6.1f%%  [%s]",
			spinner, done, total, pct, bar)
	}
}

func main() {
	fmt.Println("Starting → resolving RPC endpoint & fetching chain height…")

	start := time.Now()
	height, err := getCurrentBlockHeight()
	if err != nil {
		log.Fatalf("failed to get block height: %v", err)
	}
	fmt.Printf("Current block height: %d\n", height)

	gen, err := fetchBlock(1)
	if err != nil {
		log.Fatalf("failed to fetch genesis block: %v", err)
	}
	genesisTime, _ := time.Parse(time.RFC3339Nano, gen.Result.Block.Header.Time)

	// worker pool
	heightsCh := make(chan int, 100)
	resultsCh := make(chan *BlockResult, 100)
	var wg sync.WaitGroup
	var processed int32
	var skipped int32

	for i := 0; i < workerCount; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for h := range heightsCh {
				if res, err := processBlock(h, genesisTime); err == nil {
					resultsCh <- res
					atomic.AddInt32(&processed, 1)
				} else {
					atomic.AddInt32(&skipped, 1) // << bump skip count
				}
			}
		}()
	}
	go func() {
		for i := 1; i <= height; i++ {
			heightsCh <- i
		}
		close(heightsCh)
	}()
	go func() {
		wg.Wait()
		close(resultsCh)
	}()

	// progress ticker
	ticker := time.NewTicker(500 * time.Millisecond)
	doneCh := make(chan struct{})
	go func() {
		idx := 0
		for {
			select {
			case <-ticker.C:
				done := int(atomic.LoadInt32(&processed))
				printProgress(done, height, time.Since(start), spinnerFrames[idx%len(spinnerFrames)])
				idx++
			case <-doneCh:
				ticker.Stop()
				return
			}
		}
	}()

	// aggregate
	weeklyData := make(map[int]*WeekData)
	for res := range resultsCh {
		wi := int(res.Timestamp.Sub(genesisTime).Seconds() / (7 * 24 * 3600))
		wd, ok := weeklyData[wi]
		if !ok {
			wd = &WeekData{
				ValidatorVotes: make(map[string]int),
				ActiveUsers:    make(map[string]struct{}),
				NewUsers:       make(map[string]struct{}),
				TxCountPerUser: make(map[string]int),
			}
			weeklyData[wi] = wd
		}
		wd.Timestamps = append(wd.Timestamps, res.Timestamp)
		wd.NewEncryptedTxSubmitted += res.NewEncryptedTxSubmitted
		wd.RevertedEncryptedTx += res.RevertedEncryptedTx
		wd.NewGeneralEncryptedTxSubmitted += res.NewGeneralEncryptedTxSubmitted
		wd.KeyshareSent += res.KeyshareSent
		wd.KeyshareAggregated += res.KeyshareAggregated
		wd.GeneralKeyshareAggregated += res.GeneralKeyshareAggregated
		for v, c := range res.ValidatorVotes {
			wd.ValidatorVotes[v] += c
		}
		wd.WeeklyTotalTxs += res.TotalTxs
		// cumulativeTotalTxs += res.TotalTxs
		for user, c := range res.TxCountPerSender {
			if _, seen := allUsers[user]; !seen {
				allUsers[user] = struct{}{}
				userFirstSeenWeek[user] = wi
				wd.NewUsers[user] = struct{}{}
			}
			wd.ActiveUsers[user] = struct{}{}
			wd.TxCountPerUser[user] += c
		}
	}

	// finish progress bar
	close(doneCh)
	printProgress(int(atomic.LoadInt32(&processed)), height, time.Since(start), " ")
	fmt.Println()

	// —— weekly_stats.csv ——
	statsF, err := os.Create("weekly_stats.csv")
	if err != nil {
		log.Fatalf("couldn't create weekly_stats.csv: %v", err)
	}
	defer statsF.Close()
	w := csv.NewWriter(statsF)
	defer w.Flush()

	header := []string{
		"week_start", "avg_block_time",
		"successful_encrypted_txs", "reverted_encrypted_txs",
		"successful_general_encrypted_tx", "successful_keyshares",
		"decryption_keys", "general_decryption_keys",
		"total_users", "active_users", "new_users",
		"users_1_tx", "users_2_5_tx", "users_5plus_tx",
		"weekly_txs", "cumulative_txs",
	}
	if err := w.Write(header); err != nil {
		log.Fatalf("error writing header: %v", err)
	}

	var cumUsers, cumTxs int
	weeks := make([]int, 0, len(weeklyData))
	for wi := range weeklyData {
		weeks = append(weeks, wi)
	}
	sort.Ints(weeks)

	for _, wi := range weeks {
		wd := weeklyData[wi]
		sort.Slice(wd.Timestamps, func(i, j int) bool {
			return wd.Timestamps[i].Before(wd.Timestamps[j])
		})
		var avg float64
		if len(wd.Timestamps) > 1 {
			var sum float64
			for i := 1; i < len(wd.Timestamps); i++ {
				sum += wd.Timestamps[i].Sub(wd.Timestamps[i-1]).Seconds()
			}
			avg = sum / float64(len(wd.Timestamps)-1)
		}
		weekStart := genesisTime.Add(time.Duration(wi) * 7 * 24 * time.Hour).Format(time.RFC3339)

		newU := len(wd.NewUsers)
		activeU := len(wd.ActiveUsers)
		cumUsers += newU
		cumTxs += wd.WeeklyTotalTxs

		u1, u2_5, u5p := 0, 0, 0
		for _, cnt := range wd.TxCountPerUser {
			switch {
			case cnt == 1:
				u1++
			case cnt >= 2 && cnt <= 5:
				u2_5++
			case cnt > 5:
				u5p++
			}
		}

		row := []string{
			weekStart,
			fmt.Sprintf("%.2f", avg),
			strconv.Itoa(wd.NewEncryptedTxSubmitted),
			strconv.Itoa(wd.RevertedEncryptedTx),
			strconv.Itoa(wd.NewGeneralEncryptedTxSubmitted),
			strconv.Itoa(wd.KeyshareSent),
			strconv.Itoa(wd.KeyshareAggregated),
			strconv.Itoa(wd.GeneralKeyshareAggregated),
			strconv.Itoa(cumUsers),
			strconv.Itoa(activeU),
			strconv.Itoa(newU),
			strconv.Itoa(u1),
			strconv.Itoa(u2_5),
			strconv.Itoa(u5p),
			strconv.Itoa(wd.WeeklyTotalTxs),
			strconv.Itoa(cumTxs),
		}
		if err := w.Write(row); err != nil {
			log.Fatalf("error writing row for week %d: %v", wi, err)
		}
	}
	fmt.Println("Weekly stats written to weekly_stats.csv")

	// —— validator_votes.csv ——
	allVals := make(map[string]struct{})
	for _, wd := range weeklyData {
		for v := range wd.ValidatorVotes {
			allVals[v] = struct{}{}
		}
	}
	vals := make([]string, 0, len(allVals))
	for v := range allVals {
		vals = append(vals, v)
	}
	sort.Strings(vals)

	vF, err := os.Create("validator_votes.csv")
	if err != nil {
		log.Fatalf("couldn't create validator_votes.csv: %v", err)
	}
	defer vF.Close()
	vw := csv.NewWriter(vF)
	defer vw.Flush()

	vHeader := append([]string{"week_start"}, vals...)
	if err := vw.Write(vHeader); err != nil {
		log.Fatalf("error writing votes header: %v", err)
	}

	for _, wi := range weeks {
		wd := weeklyData[wi]
		weekStart := genesisTime.Add(time.Duration(wi) * 7 * 24 * time.Hour).Format(time.RFC3339)
		row := []string{weekStart}
		for _, v := range vals {
			row = append(row, strconv.Itoa(wd.ValidatorVotes[v]))
		}
		if err := vw.Write(row); err != nil {
			log.Fatalf("error writing votes row for week %d: %v", wi, err)
		}
	}
	fmt.Println("Validator votes written to validator_votes.csv")

	fmt.Printf("Done in %s\n", time.Since(start))
	fmt.Printf("Skipped %d blocks\n", atomic.LoadInt32(&skipped))
}
