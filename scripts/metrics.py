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

# cumulative user & tx metrics
all_users = set()                  # every account ever seen
user_first_seen_week = {}          # user → week index when they first appeared
cumulative_total_txs = 0           # lifetime tx count

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
        return int(data["result"]["sync_info"]["latest_block_height"])
    except Exception as e:
        print("Error parsing current block height:", e)
        return None

def fetch_block_results(height):
    return fetch_json(f"{BASE_URL}/block_results?height={height}")

def fetch_block(height):
    return fetch_json(f"{BASE_URL}/block?height={height}")

def parse_time(timestr):
    if timestr.endswith("Z"):
        timestr = timestr[:-1] + "+00:00"
    try:
        dot = timestr.find('.')
        if dot != -1:
            tz = timestr.find('+', dot)
            if tz == -1:
                tz = timestr.find('-', dot)
            if tz == -1:
                tz = len(timestr)
            frac = timestr[dot+1:tz]
            if len(frac) > 6:
                frac = frac[:6]
            timestr = timestr[:dot+1] + frac + timestr[tz:]
        return datetime.datetime.fromisoformat(timestr)
    except Exception as e:
        print(f"Error parsing time '{timestr}': {e}")
        return None

def main():
    global cumulative_total_txs

    current_height = get_current_block_height()
    if current_height is None:
        print("Failed to fetch current block height.")
        return
    print(f"Current block height: {current_height}")

    # get genesis time
    genesis = fetch_block(1)
    if genesis is None:
        print("Failed to fetch genesis block.")
        return
    gt = genesis.get("result", {}).get("block", {}).get("header", {}).get("time")
    if not gt:
        print("Genesis block missing timestamp.")
        return
    genesis_time = parse_time(gt)
    if genesis_time is None:
        print("Error parsing genesis time.")
        return

    # per-week aggregation
    weekly_data = {}

    for height in range(1, current_height + 1):
        print(f"Processing block {height}")
        br = fetch_block_results(height)
        blk = fetch_block(height)
        if br is None or blk is None:
            print(f"Skipping block {height} due to fetch error.")
            continue

        # timestamp → week index
        bt_str = blk["result"]["block"]["header"].get("time")
        if not bt_str:
            print(f"Block {height} missing timestamp, skipping.")
            continue
        bt = parse_time(bt_str)
        if bt is None:
            print(f"Error parsing block {height} time.")
            continue

        week_idx = int((bt - genesis_time).total_seconds() // (7*24*3600))
        if week_idx not in weekly_data:
            weekly_data[week_idx] = {
                "timestamps": [],
                # existing event counters:
                **{v: 0 for v in EVENT_TYPE_MAPPING.values()},
                "validator_votes": {},
                # new user/tx metrics:
                "active_users_set": set(),
                "new_users_set": set(),
                "tx_count_per_user": {},
                "weekly_total_txs": 0,
            }

        wd = weekly_data[week_idx]
        wd["timestamps"].append(bt)

        # process each tx
        txs = br.get("result", {}).get("txs_results") or []
        for tx in txs:
            # 4. total txs this week & lifetime
            wd["weekly_total_txs"] += 1
            cumulative_total_txs += 1

            # find all senders in this tx
            senders = set()
            for ev in tx.get("events", []):
                if ev.get("type") == "message":
                    for attr in ev.get("attributes", []):
                        if attr.get("key") == "sender":
                            senders.add(attr.get("value"))

            for sender in senders:
                if sender not in all_users:
                    all_users.add(sender)
                    user_first_seen_week[sender] = week_idx
                    wd["new_users_set"].add(sender)
                wd["active_users_set"].add(sender)
                wd["tx_count_per_user"][sender] = wd["tx_count_per_user"].get(sender, 0) + 1

            # existing event tallying
            for ev in tx.get("events", []):
                et = ev.get("type", "")
                if et in EVENT_TYPE_MAPPING:
                    wd[EVENT_TYPE_MAPPING[et]] += 1

        # validator votes
        for pc in blk["result"]["block"]["last_commit"].get("precommits", []):
            if not pc:
                continue
            val = pc.get("validator_address")
            if val:
                vd = wd["validator_votes"]
                vd[val] = vd.get(val, 0) + 1

    # === write weekly_stats.csv ===
    stats_filename = "weekly_stats.csv"
    stats_fieldnames = [
        "week_start", "avg_block_time",
        "successful_encrypted_txs", "reverted_encrypted_txs",
        "successful_general_encrypted_tx", "successful_keyshares",
        "decryption_keys", "general_decryption_keys",
        # new columns:
        "total_users", "active_users", "new_users",
        "users_1_tx", "users_2_5_tx", "users_5plus_tx",
        "weekly_txs", "cumulative_txs",
    ]

    with open(stats_filename, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=stats_fieldnames)
        writer.writeheader()

        cum_txs = 0
        cum_users = 0

        for week_idx in sorted(weekly_data):
            wd = weekly_data[week_idx]
            # avg block time
            times = sorted(wd["timestamps"])
            if len(times) > 1:
                diffs = [(t2 - t1).total_seconds() for t1, t2 in zip(times, times[1:])]
                avg_bt = sum(diffs) / len(diffs)
            else:
                avg_bt = 0

            week_start = (genesis_time + datetime.timedelta(days=7*week_idx)).isoformat()

            # user/tx aggregates
            weekly_txs = wd["weekly_total_txs"]
            cum_txs += weekly_txs
            new_u = len(wd["new_users_set"])
            cum_users += new_u
            active_u = len(wd["active_users_set"])
            tx_counts = wd["tx_count_per_user"]
            u1 = sum(1 for v in tx_counts.values() if v == 1)
            u2_5 = sum(1 for v in tx_counts.values() if 2 <= v <= 5)
            u5p = sum(1 for v in tx_counts.values() if v > 5)

            row = {
                "week_start": week_start,
                "avg_block_time": avg_bt,
                "successful_encrypted_txs": wd["new_encrypted_tx_submitted"],
                "reverted_encrypted_txs": wd["reverted_encrypted_tx"],
                "successful_general_encrypted_tx": wd["new_general_encrypted_tx_submitted"],
                "successful_keyshares": wd["keyshare_sent"],
                "decryption_keys": wd["keyshare_aggregated"],
                "general_decryption_keys": wd["general_keyshare_aggregated"],
                # new
                "total_users": cum_users,
                "active_users": active_u,
                "new_users": new_u,
                "users_1_tx": u1,
                "users_2_5_tx": u2_5,
                "users_5plus_tx": u5p,
                "weekly_txs": weekly_txs,
                "cumulative_txs": cum_txs,
            }
            writer.writerow(row)

    print(f"Weekly stats written to {stats_filename}")

    # === write validator_votes.csv (unchanged) ===
    all_validators = set()
    for wd in weekly_data.values():
        all_validators.update(wd["validator_votes"].keys())
    sorted_vals = sorted(all_validators)

    votes_filename = "validator_votes.csv"
    votes_fieldnames = ["week_start"] + sorted_vals

    with open(votes_filename, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=votes_fieldnames)
        writer.writeheader()

        for week_idx in sorted(weekly_data):
            wd = weekly_data[week_idx]
            week_start = (genesis_time + datetime.timedelta(days=7*week_idx)).isoformat()
            row = {"week_start": week_start}
            for val in sorted_vals:
                row[val] = wd["validator_votes"].get(val, 0)
            writer.writerow(row)

    print(f"Validator votes written to {votes_filename}")


if __name__ == "__main__":
    main()
