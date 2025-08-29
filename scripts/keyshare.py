import asyncio
import websockets
import json
import aiohttp

# Configuration
COSMOS_WEBSOCKET_URL = "ws://54.210.57.197:26657/websocket"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1334089287058198569/AoTKnNB-8KvLdWOJ8_Uvr06amDYGPBpLUcveHrF0Bz5GMz5ZLvE1iC3VpzcGJIx1heup"  # Replace with your webhook URL
TARGET_EVENT_TYPE = "keyshare-sent"

COSMOS_RPC_URL = "http://54.210.57.197:1317"
VALIDATOR_QUERY_PATH = "/fairyring/keyshare/validator_set"
AUTHORIZED_ADDRESSES_QUERY_PATH = "/fairyring/keyshare/authorized_address"
QUERY_INTERVAL = 100

# Global state
active_validators = []
authorized_addresses = {}  # Dictionary {validator_address: [list_of_authorized_addresses]}
missed_blocks = {}  # Dictionary to track missed block counts for validators

async def query_validator_set_periodically():
    """Periodically fetch the validator set and authorized addresses."""
    while True:
        await fetch_validator_set()
        await fetch_authorized_addresses()
        await asyncio.sleep(QUERY_INTERVAL)

async def fetch_validator_set():
    """Fetch the active validators from the chain."""
    global active_validators
    url = f"{COSMOS_RPC_URL}{VALIDATOR_QUERY_PATH}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    update_active_validators(data)
                else:
                    print(f"Failed to fetch validator set. Status code: {response.status}")
        except Exception as e:
            print(f"Error fetching validator set: {e}")

async def fetch_authorized_addresses():
    """Fetch the authorized addresses from the chain."""
    global authorized_addresses
    url = f"{COSMOS_RPC_URL}{AUTHORIZED_ADDRESSES_QUERY_PATH}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    update_authorized_addresses(data)
                else:
                    print(f"Failed to fetch authorized addresses. Status code: {response.status}")
        except Exception as e:
            print(f"Error fetching authorized addresses: {e}")

def update_active_validators(data):
    """Update the active validators list."""
    global active_validators, missed_blocks

    if "validator_set" in data:
        validator_set = data["validator_set"]
        new_validators = [
            validator["validator"]
            for validator in validator_set
            if validator["is_active"]
        ]
        active_validators = list(set(new_validators))  # Update global active validators

        # Ensure missed_blocks has an entry for each active validator
        for validator in active_validators:
            if validator not in missed_blocks:
                missed_blocks[validator] = 0

        print(f"Updated active validators: {active_validators}")
    else:
        print("No validator set found in response.")

def update_authorized_addresses(data):
    """Update the authorized address mapping."""
    global authorized_addresses

    if "authorized_address" in data:
        authorized_addresses.clear()
        for entry in data["authorized_address"]:
            if entry["is_authorized"]:
                validator = entry["authorized_by"]
                if validator not in authorized_addresses:
                    authorized_addresses[validator] = []
                authorized_addresses[validator].append(entry["target"])

        print(f"Updated authorized addresses: {authorized_addresses}")
    else:
        print("No authorized addresses found in response.")

async def listen_to_tx_events():
    """Listen for transaction events and process them."""
    async with websockets.connect(COSMOS_WEBSOCKET_URL) as ws:
        # Subscribe to transaction events
        subscribe_msg = {
            "jsonrpc": "2.0",
            "method": "subscribe",
            "id": 1,
            "params": {
                "query": "tm.event = 'Tx'"
            }
        }
        await ws.send(json.dumps(subscribe_msg))

        print("Listening for transaction events...")

        while True:
            response = json.loads(await ws.recv())
            if "result" in response and "data" in response["result"]:
                tx_data = response["result"]["data"]
                events = tx_data.get("value", {}).get("TxResult", {}).get("result", {}).get("events", [])
                process_events(events)

def process_events(events):
    """Process received transaction events and track missing transactions."""
    global missed_blocks

    if not active_validators:
        print("Validator list is empty. Skipping event processing.")
        return

    # Track which validators (or their authorized accounts) had events
    validators_with_events = set()

    for event in events:
        if event.get("type") == TARGET_EVENT_TYPE:
            attributes = {attr["key"]: attr["value"] for attr in event.get("attributes", [])}
            sender = attributes.get("validator")  # The account that submitted the TX

            # Check if the sender is a validator or an authorized address
            for validator, auth_addresses in authorized_addresses.items():
                if sender == validator or sender in auth_addresses:
                    validators_with_events.add(validator)
                    break

    # Update missed block counters
    for validator in active_validators:
        if validator not in validators_with_events:
            missed_blocks[validator] += 1
            if missed_blocks[validator] >= 50:  # Notify if missed blocks reach threshold
                asyncio.create_task(notify_discord(validator))
                missed_blocks[validator] = 0
        else:
            missed_blocks[validator] = 0  # Reset counter if validator or an authorized address has an event

    print(f"Missed block counters: {missed_blocks}")

async def notify_discord(validator):
    """Send a Discord notification if a validator has missed 50 consecutive blocks."""
    async with aiohttp.ClientSession() as session:
        message = {
            "content": f"Validator `{validator}` has missed sending keyshares in 50 consecutive blocks!"
        }
        async with session.post(DISCORD_WEBHOOK_URL, json=message) as response:
            if response.status == 204:
                print(f"Notification sent for validator: {validator}")
            else:
                print(f"Failed to send notification. Status: {response.status}")

async def main():
    try:
        # Run both tasks concurrently
        await asyncio.gather(
            listen_to_tx_events(),       # Task to listen for transaction events
            query_validator_set_periodically()  # Task to query the validator set and authorized addresses
        )
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
