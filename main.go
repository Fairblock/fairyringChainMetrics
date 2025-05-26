package main

import (
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
	"sync"
	"time"
)

const baseURL = "http://54.210.57.197:26657"
const workerCount = 500 // or lower this number if needed

// Create a shared HTTP client with a custom transport
var client = &http.Client{
	Transport: &http.Transport{
		DialContext: (&net.Dialer{
			Timeout:   30 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		MaxIdleConns:        1000,
		MaxIdleConnsPerHost: 1000,
	},
	Timeout: 30 * time.Second,
}

// Structures for /status response.
type StatusResponse struct {
	Result struct {
		SyncInfo struct {
			LatestBlockHeight string `json:"latest_block_height"`
		} `json:"sync_info"`
	} `json:"result"`
}

// Structures for /block response.
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

// Structures for /block_results response.
type BlockResultsResponse struct {
	Result struct {
		TxsResults []struct {
			Events []struct {
				Type string `json:"type"`
			} `json:"events"`
		} `json:"txs_results"`
	} `json:"result"`
}

// BlockResult holds data extracted from a block.
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
}

// WeekData aggregates data for a week.
type WeekData struct {
	Timestamps                     []time.Time
	NewEncryptedTxSubmitted        int
	RevertedEncryptedTx            int
	NewGeneralEncryptedTxSubmitted int
	KeyshareSent                   int
	KeyshareAggregated             int
	GeneralKeyshareAggregated      int
	ValidatorVotes                 map[string]int
}

func getCurrentBlockHeight() (int, error) {
	url := fmt.Sprintf("%s/status", baseURL)
	resp, err := client.Get(url)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return 0, err
	}
	var status StatusResponse
	if err := json.Unmarshal(body, &status); err != nil {
		return 0, err
	}
	height, err := strconv.Atoi(status.Result.SyncInfo.LatestBlockHeight)
	if err != nil {
		return 0, err
	}
	return height, nil
}

func fetchBlock(height int) (*BlockResponse, error) {
	url := fmt.Sprintf("%s/block?height=%d", baseURL, height)
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var blockResp BlockResponse
	if err := json.Unmarshal(body, &blockResp); err != nil {
		return nil, err
	}
	return &blockResp, nil
}

func fetchBlockResults(height int) (*BlockResultsResponse, error) {
	url := fmt.Sprintf("%s/block_results?height=%d", baseURL, height)
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var br BlockResultsResponse
	if err := json.Unmarshal(body, &br); err != nil {
		return nil, err
	}
	return &br, nil
}

// processBlock fetches the block and its block_results, parses the data and returns a BlockResult.
func processBlock(height int, genesisTime time.Time) (*BlockResult, error) {
	blockResp, err := fetchBlock(height)
	if err != nil {
		return nil, fmt.Errorf("error fetching block %d: %v", height, err)
	}
	brResp, err := fetchBlockResults(height)
	if err != nil {
		return nil, fmt.Errorf("error fetching block_results for block %d: %v", height, err)
	}

	// Parse timestamp using RFC3339Nano (Go supports nanosecond precision).
	blockTime, err := time.Parse(time.RFC3339Nano, blockResp.Result.Block.Header.Time)
	if err != nil {
		return nil, fmt.Errorf("error parsing timestamp for block %d: %v", height, err)
	}

	result := &BlockResult{
		Height:         height,
		Timestamp:      blockTime,
		ValidatorVotes: make(map[string]int),
	}

	// Process tx events.
	if brResp.Result.TxsResults != nil {
		for _, tx := range brResp.Result.TxsResults {
			for _, event := range tx.Events {
				switch event.Type {
				case "new-encrypted-tx-submitted":
					result.NewEncryptedTxSubmitted++
				case "reverted-encrypted-tx":
					result.RevertedEncryptedTx++
				case "new-general-encrypted-tx-submitted":
					result.NewGeneralEncryptedTxSubmitted++
				case "keyshare-sent":
					result.KeyshareSent++
				case "keyshare-aggregated":
					result.KeyshareAggregated++
				case "general-keyshare-aggregated":
					result.GeneralKeyshareAggregated++
				}
			}
		}
	}

	// Process validator votes from last_commit.precommits.
	if precommits := blockResp.Result.Block.LastCommit.Precommits; precommits != nil {
		for _, precommit := range precommits {
			if precommit == nil {
				continue
			}
			if addr := precommit.ValidatorAddress; addr != "" {
				result.ValidatorVotes[addr]++
			}
		}
	}

	return result, nil
}

func main() {
	start := time.Now()

	// Get current block height.
	currentHeight, err := getCurrentBlockHeight()
	if err != nil {
		log.Fatalf("Error fetching current block height: %v", err)
	}
	fmt.Printf("Current block height: %d\n", currentHeight)

	// Get genesis time from block 1.
	genesisBlock, err := fetchBlock(1)
	if err != nil {
		log.Fatalf("Error fetching genesis block: %v", err)
	}
	genesisTime, err := time.Parse(time.RFC3339Nano, genesisBlock.Result.Block.Header.Time)
	if err != nil {
		log.Fatalf("Error parsing genesis block time: %v", err)
	}
	fmt.Printf("Genesis time: %s\n", genesisTime.Format(time.RFC3339))

	// Set up channels and worker pool.
	heightsChan := make(chan int, 100)
	resultsChan := make(chan *BlockResult, 100)
	var wg sync.WaitGroup

	worker := func() {
		defer wg.Done()
		for height := range heightsChan {
			res, err := processBlock(height, genesisTime)
			if err != nil {
				fmt.Printf("Error processing block %d: %v\n", height, err)
				continue
			}
			resultsChan <- res
		}
	}

	// Launch workers.
	for i := 0; i < workerCount; i++ {
		wg.Add(1)
		go worker()
	}

	// Send block heights to process.
	go func() {
		for h := 1; h <= currentHeight; h++ {
			heightsChan <- h
		}
		close(heightsChan)
	}()

	// Close resultsChan once all workers are done.
	go func() {
		wg.Wait()
		close(resultsChan)
	}()

	// Aggregate weekly data.
	weeklyData := make(map[int]*WeekData)
	processedBlocks := 0
	for res := range resultsChan {
		processedBlocks++
		if processedBlocks%1000 == 0 {
			fmt.Printf("Processed %d blocks so far...\n", processedBlocks)
		}
		weekIndex := int(res.Timestamp.Sub(genesisTime).Seconds() / (7 * 24 * 3600))
		wd, exists := weeklyData[weekIndex]
		if !exists {
			wd = &WeekData{
				ValidatorVotes: make(map[string]int),
			}
			weeklyData[weekIndex] = wd
		}
		wd.Timestamps = append(wd.Timestamps, res.Timestamp)
		wd.NewEncryptedTxSubmitted += res.NewEncryptedTxSubmitted
		wd.RevertedEncryptedTx += res.RevertedEncryptedTx
		wd.NewGeneralEncryptedTxSubmitted += res.NewGeneralEncryptedTxSubmitted
		wd.KeyshareSent += res.KeyshareSent
		wd.KeyshareAggregated += res.KeyshareAggregated
		wd.GeneralKeyshareAggregated += res.GeneralKeyshareAggregated
		for v, count := range res.ValidatorVotes {
			wd.ValidatorVotes[v] += count
		}
	}

	// Write weekly_stats.csv.
	statsFile, err := os.Create("weekly_stats.csv")
	if err != nil {
		log.Fatalf("Error creating weekly_stats.csv: %v", err)
	}
	defer statsFile.Close()
	statsWriter := csv.NewWriter(statsFile)
	defer statsWriter.Flush()

	header := []string{"week_start", "avg_block_time", "successful_encrypted_txs", "reverted_encrypted_txs", "successful_general_encrypted_tx", "successful_keyshares", "decryption_keys", "general_decryption_keys"}
	if err := statsWriter.Write(header); err != nil {
		log.Fatalf("Error writing header to weekly_stats.csv: %v", err)
	}

	for weekIndex, wd := range weeklyData {
		sort.Slice(wd.Timestamps, func(i, j int) bool { return wd.Timestamps[i].Before(wd.Timestamps[j]) })
		var avgBlockTime float64
		if len(wd.Timestamps) > 1 {
			var sumDiff float64
			for i := 1; i < len(wd.Timestamps); i++ {
				sumDiff += wd.Timestamps[i].Sub(wd.Timestamps[i-1]).Seconds()
			}
			avgBlockTime = sumDiff / float64(len(wd.Timestamps)-1)
		}
		weekStart := genesisTime.Add(time.Duration(weekIndex) * 7 * 24 * time.Hour).Format(time.RFC3339)
		row := []string{
			weekStart,
			fmt.Sprintf("%.2f", avgBlockTime),
			strconv.Itoa(wd.NewEncryptedTxSubmitted),
			strconv.Itoa(wd.RevertedEncryptedTx),
			strconv.Itoa(wd.NewGeneralEncryptedTxSubmitted),
			strconv.Itoa(wd.KeyshareSent),
			strconv.Itoa(wd.KeyshareAggregated),
			strconv.Itoa(wd.GeneralKeyshareAggregated),
		}
		if err := statsWriter.Write(row); err != nil {
			log.Fatalf("Error writing row to weekly_stats.csv: %v", err)
		}
	}
	fmt.Println("Weekly stats written to weekly_stats.csv")

	allValidators := make(map[string]struct{})
	for _, wd := range weeklyData {
		for v := range wd.ValidatorVotes {
			allValidators[v] = struct{}{}
		}
	}
	validators := make([]string, 0, len(allValidators))
	for v := range allValidators {
		validators = append(validators, v)
	}
	sort.Strings(validators)

	votesFile, err := os.Create("validator_votes.csv")
	if err != nil {
		log.Fatalf("Error creating validator_votes.csv: %v", err)
	}
	defer votesFile.Close()
	votesWriter := csv.NewWriter(votesFile)
	defer votesWriter.Flush()

	vHeader := []string{"week_start"}
	vHeader = append(vHeader, validators...)
	if err := votesWriter.Write(vHeader); err != nil {
		log.Fatalf("Error writing header to validator_votes.csv: %v", err)
	}

	for weekIndex, wd := range weeklyData {
		weekStart := genesisTime.Add(time.Duration(weekIndex) * 7 * 24 * time.Hour).Format(time.RFC3339)
		row := []string{weekStart}
		for _, v := range validators {
			row = append(row, strconv.Itoa(wd.ValidatorVotes[v]))
		}
		if err := votesWriter.Write(row); err != nil {
			log.Fatalf("Error writing row to validator_votes.csv: %v", err)
		}
	}
	fmt.Println("Validator votes written to validator_votes.csv")

	elapsed := time.Since(start)
	fmt.Printf("Processing completed in %s\n", elapsed)
}
