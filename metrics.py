import requests
import datetime
import csv
import time

BASE_URL = "http://54.210.57.197:26657"

# Map the event types to our internal counter names.
EVENT_TYPE_MAPPING = {
    "new-encrypted-tx-submitted": "new_encrypted_tx_submitted",
    "reverted-encrypted-tx": "reverted_encrypted_tx",
    "new-general-encrypted-tx-submitted": "new_general_encrypted_tx_submitted",
    "keyshare-sent": "keyshare_sent",
    "keyshare-aggregated": "keyshare_aggregated",
    "general-keyshare-aggregated": "general_keyshare_aggregated"
}


def fetch_json(url):
    try:
        response = requests.get(url)
        if response.status_code != 200:
            print(f"Error: HTTP {response.status_code} for URL {url}")
            return None
        return response.json()
    except Exception as e:
        print(f"Exception fetching {url}: {e}")
        return None


def get_current_block_height():
    url = f"{BASE_URL}/status"
    data = fetch_json(url)
    if data is None:
        return None
    try:
        height = int(data["result"]["sync_info"]["latest_block_height"])
        return height
    except Exception as e:
        print("Error parsing current block height:", e)
        return None


def fetch_block_results(height):
    url = f"{BASE_URL}/block_results?height={height}"
    return fetch_json(url)


def fetch_block(height):
    url = f"{BASE_URL}/block?height={height}"
    return fetch_json(url)


def parse_time(timestr):
    """
    Parses an RFC3339 timestamp that might contain nanosecond precision.
    This function truncates the fractional seconds to microseconds (6 digits)
    so that Python's fromisoformat can parse it.
    """
    # If timestr ends with 'Z', replace it with '+00:00'
    if timestr.endswith("Z"):
        timestr = timestr[:-1] + "+00:00"
    try:
        dot_index = timestr.find('.')
        if dot_index != -1:
            # Find where the timezone part starts (look for '+' or '-' after the fractional part)
            tz_index = timestr.find('+', dot_index)
            if tz_index == -1:
                tz_index = timestr.find('-', dot_index)
            if tz_index == -1:
                tz_index = len(timestr)
            fractional = timestr[dot_index+1:tz_index]
            # Truncate fractional part to microseconds (6 digits)
            if len(fractional) > 6:
                fractional = fractional[:6]
            timestr = timestr[:dot_index+1] + fractional + timestr[tz_index:]
        return datetime.datetime.fromisoformat(timestr)
    except Exception as e:
        print(f"Error parsing time '{timestr}': {e}")
        return None


def main():
    current_height = get_current_block_height()
    if current_height is None:
        print("Failed to fetch current block height.")
        return

    print(f"Current block height: {current_height}")

    # Fetch the genesis block (assumed at height 1) to get the genesis timestamp.
    genesis_block = fetch_block(1)
    if genesis_block is None:
        print("Failed to fetch genesis block.")
        return
    genesis_time_str = (
        genesis_block.get("result", {})
        .get("block", {})
        .get("header", {})
        .get("time")
    )
    if not genesis_time_str:
        print("Genesis block missing timestamp.")
        return
    genesis_time = parse_time(genesis_time_str)
    if genesis_time is None:
        print("Error parsing genesis time.")
        return

    # Store weekly aggregated data in a dict keyed by week_index.
    # Each entry will have:
    #   - a list of block timestamps (to compute average block time)
    #   - counters for our events
    #   - a dict for validator votes (keyed by validator address)
    weekly_data = {}

    # Iterate over every block.
    for height in range(1, current_height + 1):
        print(f"Processing block {height}")

        # Fetch block_results (contains tx events).
        block_results = fetch_block_results(height)
        if block_results is None:
            print(f"Skipping block {height} due to error in block_results.")
            continue

        # Fetch block (contains header info and last_commit for validator votes).
        block = fetch_block(height)
        if block is None:
            print(f"Skipping block {height} due to error in block data.")
            continue

        # Parse block timestamp.
        block_time_str = (
            block.get("result", {})
            .get("block", {})
            .get("header", {})
            .get("time")
        )
        if not block_time_str:
            print(f"Block {height} missing timestamp, skipping.")
            continue
        block_time = parse_time(block_time_str)
        if block_time is None:
            print(f"Error parsing timestamp for block {height}.")
            continue

        # Determine which week this block belongs to based on genesis_time.
        delta = block_time - genesis_time
        week_index = int(delta.total_seconds() // (7 * 24 * 3600))
        if week_index not in weekly_data:
            weekly_data[week_index] = {
                "timestamps": [],
                "new_encrypted_tx_submitted": 0,
                "reverted_encrypted_tx": 0,
                "new_general_encrypted_tx_submitted": 0,
                "keyshare_sent": 0,
                "keyshare_aggregated": 0,
                "general_keyshare_aggregated": 0,
                "validator_votes": {}
            }

        weekly_data[week_index]["timestamps"].append(block_time)

        # Process tx events from the block_results.
        txs_results = block_results.get("result", {}).get("txs_results")
        if not txs_results:
            txs_results = []  # if empty or None, use an empty list

        for tx in txs_results:
            events = tx.get("events", [])
            for event in events:
                event_type = event.get("type", "")
                if event_type in EVENT_TYPE_MAPPING:
                    counter_name = EVENT_TYPE_MAPPING[event_type]
                    weekly_data[week_index][counter_name] += 1

        # Process validator votes from the block.
        precommits = (
            block.get("result", {})
            .get("block", {})
            .get("last_commit", {})
            .get("precommits", [])
        )
        for precommit in precommits:
            if precommit is None:
                continue
            validator_address = precommit.get("validator_address")
            if not validator_address:
                continue
            votes = weekly_data[week_index]["validator_votes"]
            votes[validator_address] = votes.get(validator_address, 0) + 1

        # # Optional: sleep briefly to avoid overloading the endpoint.
        # time.sleep(0.1)

    # Write the first CSV: weekly_stats.csv.
    stats_filename = "weekly_stats.csv"
    # CSV columns: week_start, avg_block_time, successful_encrypted_txs, reverted_encrypted_txs,
    # successful_general_encrypted_tx, successful_keyshares, decryption_keys, general_decryption_keys.
    stats_fieldnames = [
        "week_start",
        "avg_block_time",
        "successful_encrypted_txs",
        "reverted_encrypted_txs",
        "successful_general_encrypted_tx",
        "successful_keyshares",
        "decryption_keys",
        "general_decryption_keys",
    ]

    with open(stats_filename, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=stats_fieldnames)
        writer.writeheader()

        for week_index in sorted(weekly_data.keys()):
            week_info = weekly_data[week_index]
            timestamps = sorted(week_info["timestamps"])
            # Compute average block time for the week.
            if len(timestamps) > 1:
                diffs = [
                    (t2 - t1).total_seconds()
                    for t1, t2 in zip(timestamps, timestamps[1:])
                ]
                avg_block_time = sum(diffs) / len(diffs)
            else:
                avg_block_time = 0

            week_start = (genesis_time + datetime.timedelta(days=week_index * 7)).isoformat()

            row = {
                "week_start": week_start,
                "avg_block_time": avg_block_time,
                "successful_encrypted_txs": week_info["new_encrypted_tx_submitted"],
                "reverted_encrypted_txs": week_info["reverted_encrypted_tx"],
                "successful_general_encrypted_tx": week_info["new_general_encrypted_tx_submitted"],
                "successful_keyshares": week_info["keyshare_sent"],
                "decryption_keys": week_info["keyshare_aggregated"],
                "general_decryption_keys": week_info["general_keyshare_aggregated"],
            }
            writer.writerow(row)

    print(f"Weekly stats written to {stats_filename}")

    # Write the second CSV: validator_votes.csv.
    all_validators = set()
    for week_info in weekly_data.values():
        all_validators.update(week_info["validator_votes"].keys())
    sorted_validators = sorted(all_validators)

    votes_filename = "validator_votes.csv"
    votes_fieldnames = ["week_start"] + sorted_validators

    with open(votes_filename, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=votes_fieldnames)
        writer.writeheader()

        for week_index in sorted(weekly_data.keys()):
            week_info = weekly_data[week_index]
            week_start = (genesis_time + datetime.timedelta(days=week_index * 7)).isoformat()
            row = {"week_start": week_start}
            for validator in sorted_validators:
                row[validator] = week_info["validator_votes"].get(validator, 0)
            writer.writerow(row)

    print(f"Validator votes written to {votes_filename}")


if __name__ == "__main__":
    main()
