"""
ENSO Top Stakers Report
========================
Builds a ranked list of the biggest stakers by net staked amount,
including per-position unlock timers.

Outputs:
  - enso_top_stakers.csv      (per-owner summary)
  - enso_positions.csv        (per-position detail)
  - Prints a formatted table to stdout
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests
import pandas as pd
from dotenv import load_dotenv
from web3 import Web3

# ── Config ───────────────────────────────────────────────────────────────────
load_dotenv()

API_KEY = os.getenv("ETHERSCAN_API_KEY")
if not API_KEY:
    sys.exit("Error: ETHERSCAN_API_KEY not found in .env")

CONTRACT = "0x22Ad2a46d317C5eDF6c01fea16d4399C912E9A01"
DECIMALS = 18
BASE_URL = "https://api.etherscan.io/v2/api"
NOW = int(datetime.now(timezone.utc).timestamp())

# ── Event topics ─────────────────────────────────────────────────────────────
TOPIC_POSITION_CREATED = "0x" + Web3.keccak(
    text="PositionCreated(uint256,uint64,bytes32)"
).hex()
TOPIC_FUNDS_DEPOSITED = "0x" + Web3.keccak(
    text="FundsDeposited(uint256,uint256,uint256)"
).hex()
TOPIC_FUNDS_WITHDRAWN = "0x" + Web3.keccak(
    text="FundsWithdrawn(uint256,uint256)"
).hex()
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

ZERO_TOPIC = "0x" + "0" * 64


# ── Helpers ──────────────────────────────────────────────────────────────────
def fetch_logs(topic0: str, extra_params: dict | None = None) -> list[dict]:
    all_logs: list[dict] = []
    from_block = 0
    while True:
        params = {
            "chainid": "1",
            "module": "logs",
            "action": "getLogs",
            "address": CONTRACT,
            "topic0": topic0,
            "fromBlock": from_block,
            "toBlock": "latest",
            "apikey": API_KEY,
        }
        if extra_params:
            params.update(extra_params)
        resp = requests.get(BASE_URL, params=params, timeout=30).json()
        if resp.get("status") != "1" or not resp.get("result"):
            break
        logs = resp["result"]
        all_logs.extend(logs)
        if len(logs) >= 1000:
            from_block = int(logs[-1]["blockNumber"], 16) + 1
            time.sleep(0.25)
        else:
            break
    return all_logs


def h(val: str) -> int:
    return int(val, 16)


def decode_word(data: str, index: int) -> int:
    start = 2 + index * 64
    return int(data[start : start + 64], 16)


def addr_from_topic(topic: str) -> str:
    return Web3.to_checksum_address("0x" + topic[-40:])


def format_unlock(expiry_ts: int) -> str:
    if expiry_ts <= NOW:
        return "UNLOCKED"
    remaining = expiry_ts - NOW
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h"


# ── Data collection ──────────────────────────────────────────────────────────
def main():
    print("ENSO Top Stakers Report")
    print("=" * 60)

    # 1. PositionCreated → positionId, expiry, validatorId
    print("  Fetching PositionCreated events...")
    pc_logs = fetch_logs(TOPIC_POSITION_CREATED)
    print(f"    → {len(pc_logs):,} positions created")
    time.sleep(0.25)

    positions: dict[int, dict] = {}
    for log in pc_logs:
        pid = h(log["topics"][1])
        expiry = h(log["data"][2:66])  # uint64 expiry in data
        validator_id = bytes.fromhex(log["topics"][2][2:]).rstrip(b"\x00").decode(
            "utf-8", errors="replace"
        )
        positions[pid] = {
            "position_id": pid,
            "expiry_ts": expiry,
            "expiry_utc": datetime.fromtimestamp(expiry, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            ),
            "unlock_remaining": format_unlock(expiry),
            "validator": validator_id,
            "owner": None,
            "net_deposited": 0.0,
            "stake": 0.0,
        }

    # 2. Transfer events → ownership mapping (last Transfer wins)
    print("  Fetching Transfer events...")
    tr_logs = fetch_logs(TOPIC_TRANSFER)
    print(f"    → {len(tr_logs):,} transfers")
    time.sleep(0.25)

    for log in tr_logs:
        to_addr = addr_from_topic(log["topics"][2])
        token_id = h(log["topics"][3])
        if token_id in positions:
            positions[token_id]["owner"] = to_addr

    # 3. FundsDeposited → add to position balance
    print("  Fetching FundsDeposited events...")
    dep_logs = fetch_logs(TOPIC_FUNDS_DEPOSITED)
    print(f"    → {len(dep_logs):,} deposit events")
    time.sleep(0.25)

    for log in dep_logs:
        pid = h(log["topics"][1])
        funds_added = decode_word(log["data"], 0) / 10**DECIMALS
        stake_added = decode_word(log["data"], 1) / 10**DECIMALS
        if pid in positions:
            positions[pid]["net_deposited"] += funds_added
            positions[pid]["stake"] += stake_added

    # 4. FundsWithdrawn → subtract from position balance
    print("  Fetching FundsWithdrawn events...")
    wth_logs = fetch_logs(TOPIC_FUNDS_WITHDRAWN)
    print(f"    → {len(wth_logs):,} withdrawal events")

    for log in wth_logs:
        pid = h(log["topics"][1])
        funds_removed = decode_word(log["data"], 0) / 10**DECIMALS
        if pid in positions:
            positions[pid]["net_deposited"] -= funds_removed

    # ── Build DataFrames ─────────────────────────────────────────────────
    pos_df = pd.DataFrame(positions.values())
    pos_df = pos_df[pos_df["net_deposited"] > 0].copy()  # only active positions

    # Per-position CSV
    pos_df.sort_values("net_deposited", ascending=False, inplace=True)
    pos_df.to_csv("enso_positions.csv", index=False)
    print(f"\n  Positions CSV saved → enso_positions.csv ({len(pos_df):,} active)")

    # ── Aggregate by owner ───────────────────────────────────────────────
    owner_groups = pos_df.groupby("owner")
    owner_df = owner_groups.agg(
        total_staked=("net_deposited", "sum"),
        total_stake_weight=("stake", "sum"),
        num_positions=("position_id", "count"),
        earliest_unlock=("expiry_ts", "min"),
        latest_unlock=("expiry_ts", "max"),
    ).reset_index()

    owner_df.sort_values("total_staked", ascending=False, inplace=True)
    owner_df.reset_index(drop=True, inplace=True)
    owner_df.index += 1  # 1-based rank
    owner_df.index.name = "rank"

    # Readable unlock columns
    owner_df["earliest_unlock_utc"] = owner_df["earliest_unlock"].apply(
        lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
    )
    owner_df["latest_unlock_utc"] = owner_df["latest_unlock"].apply(
        lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
    )
    owner_df["earliest_unlock_remaining"] = owner_df["earliest_unlock"].apply(
        format_unlock
    )
    owner_df["latest_unlock_remaining"] = owner_df["latest_unlock"].apply(
        format_unlock
    )

    # Save CSV
    csv_cols = [
        "owner", "total_staked", "total_stake_weight", "num_positions",
        "earliest_unlock_utc", "earliest_unlock_remaining",
        "latest_unlock_utc", "latest_unlock_remaining",
    ]
    owner_df[csv_cols].to_csv("enso_top_stakers.csv")
    print(f"  Top stakers CSV saved → enso_top_stakers.csv ({len(owner_df):,} stakers)")

    # ── Print formatted table ────────────────────────────────────────────
    total_staked_all = owner_df["total_staked"].sum()
    print(f"\n{'═' * 120}")
    print(f"  TOP ENSO STAKERS — {len(owner_df):,} unique addresses — "
          f"{total_staked_all:,.2f} ENSO total staked")
    print(f"{'═' * 120}")
    print(
        f"{'Rank':>4}  {'Address':<44} {'Staked':>14} {'% Share':>8} "
        f"{'Pos':>4}  {'Earliest Unlock':<18} {'Latest Unlock':<18}"
    )
    print(f"{'─' * 120}")

    top_n = min(30, len(owner_df))
    for rank, row in owner_df.head(top_n).iterrows():
        pct = row["total_staked"] / total_staked_all * 100
        print(
            f"{rank:>4}  {row['owner']:<44} "
            f"{row['total_staked']:>14,.2f} {pct:>7.2f}% "
            f"{row['num_positions']:>4}  "
            f"{row['earliest_unlock_utc']} ({row['earliest_unlock_remaining']:<7}) "
            f"{row['latest_unlock_utc']} ({row['latest_unlock_remaining']:<7})"
        )

    if len(owner_df) > top_n:
        remaining_staked = owner_df.iloc[top_n:]["total_staked"].sum()
        remaining_count = len(owner_df) - top_n
        pct = remaining_staked / total_staked_all * 100
        print(
            f"{'':>4}  {'... ' + str(remaining_count) + ' more stakers':<44} "
            f"{remaining_staked:>14,.2f} {pct:>7.2f}%"
        )

    print(f"{'═' * 120}")


if __name__ == "__main__":
    main()
