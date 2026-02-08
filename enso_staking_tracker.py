"""
ENSO Staking Tracker
====================
Fetches on-chain staking events from the stENSO contract and produces:
  - enso_staking_events.csv   (raw event log)
  - enso_staking_chart.png    (cumulative staked / daily volume chart)

Contract : 0x22Ad2a46d317C5eDF6c01fea16d4399C912E9A01 (stENSO proxy)
Events   : FundsDeposited, FundsWithdrawn, RewardsIssued, RewardsWithdrawn
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
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
OUTPUT_CSV = "enso_staking_events.csv"
OUTPUT_CHART = "enso_staking_chart.png"

# ── Event topic hashes ───────────────────────────────────────────────────────
TOPICS = {
    "PositionCreated": "0x" + Web3.keccak(text="PositionCreated(uint256,uint64,bytes32)").hex(),
    "FundsDeposited":  "0x" + Web3.keccak(text="FundsDeposited(uint256,uint256,uint256)").hex(),
    "FundsWithdrawn":  "0x" + Web3.keccak(text="FundsWithdrawn(uint256,uint256)").hex(),
    "RewardsIssued":   "0x" + Web3.keccak(text="RewardsIssued(bytes32,uint256)").hex(),
    "RewardsWithdrawn": "0x" + Web3.keccak(text="RewardsWithdrawn(address,uint256)").hex(),
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def fetch_logs(topic0: str) -> list[dict]:
    """Fetch all event logs matching topic0, handling Etherscan's 1 000-row pages."""
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
        resp = requests.get(BASE_URL, params=params, timeout=30).json()

        if resp.get("status") != "1" or not resp.get("result"):
            break

        logs = resp["result"]
        all_logs.extend(logs)

        if len(logs) >= 1000:
            from_block = int(logs[-1]["blockNumber"], 16) + 1
            time.sleep(0.25)          # respect rate limits
        else:
            break

    return all_logs


def hex_to_int(h: str) -> int:
    return int(h, 16)


def decode_word(data: str, index: int) -> int:
    """Decode the Nth 32-byte word from a hex data string (0x-prefixed)."""
    start = 2 + index * 64
    return int(data[start : start + 64], 16)


# ── Event parsers ────────────────────────────────────────────────────────────
def parse_deposits(logs: list[dict]) -> list[dict]:
    """FundsDeposited(uint256 indexed positionId, uint256 fundsAdded, uint256 stakeAdded)"""
    rows = []
    for log in logs:
        position_id = hex_to_int(log["topics"][1])
        funds_added = decode_word(log["data"], 0)
        stake_added = decode_word(log["data"], 1)
        rows.append({
            "block":       hex_to_int(log["blockNumber"]),
            "timestamp":   hex_to_int(log["timeStamp"]),
            "tx_hash":     log["transactionHash"],
            "event":       "FundsDeposited",
            "position_id": position_id,
            "amount_raw":  funds_added,
            "amount":      funds_added / 10**DECIMALS,
            "stake_raw":   stake_added,
            "stake":       stake_added / 10**DECIMALS,
            "net_flow":    funds_added / 10**DECIMALS,   # positive = inflow
        })
    return rows


def parse_withdrawals(logs: list[dict]) -> list[dict]:
    """FundsWithdrawn(uint256 indexed positionId, uint256 fundsRemoved)"""
    rows = []
    for log in logs:
        position_id = hex_to_int(log["topics"][1])
        funds_removed = decode_word(log["data"], 0)
        rows.append({
            "block":       hex_to_int(log["blockNumber"]),
            "timestamp":   hex_to_int(log["timeStamp"]),
            "tx_hash":     log["transactionHash"],
            "event":       "FundsWithdrawn",
            "position_id": position_id,
            "amount_raw":  funds_removed,
            "amount":      funds_removed / 10**DECIMALS,
            "stake_raw":   0,
            "stake":       0.0,
            "net_flow":    -(funds_removed / 10**DECIMALS),   # negative = outflow
        })
    return rows


def parse_rewards_issued(logs: list[dict]) -> list[dict]:
    """RewardsIssued(bytes32 indexed validatorId, uint256 amount)"""
    rows = []
    for log in logs:
        validator_id = log["topics"][1]
        amount = decode_word(log["data"], 0)
        rows.append({
            "block":       hex_to_int(log["blockNumber"]),
            "timestamp":   hex_to_int(log["timeStamp"]),
            "tx_hash":     log["transactionHash"],
            "event":       "RewardsIssued",
            "position_id": None,
            "amount_raw":  amount,
            "amount":      amount / 10**DECIMALS,
            "stake_raw":   0,
            "stake":       0.0,
            "net_flow":    0.0,    # rewards don't change principal staked
        })
    return rows


def parse_rewards_withdrawn(logs: list[dict]) -> list[dict]:
    """RewardsWithdrawn(address indexed to, uint256 rewards)"""
    rows = []
    for log in logs:
        to_addr = "0x" + log["topics"][1][-40:]
        amount = decode_word(log["data"], 0)
        rows.append({
            "block":       hex_to_int(log["blockNumber"]),
            "timestamp":   hex_to_int(log["timeStamp"]),
            "tx_hash":     log["transactionHash"],
            "event":       "RewardsWithdrawn",
            "position_id": None,
            "amount_raw":  amount,
            "amount":      amount / 10**DECIMALS,
            "stake_raw":   0,
            "stake":       0.0,
            "net_flow":    0.0,
        })
    return rows


# ── Main pipeline ────────────────────────────────────────────────────────────
def main():
    print("ENSO Staking Tracker")
    print("=" * 50)

    # 1. Fetch events
    all_rows: list[dict] = []

    for event_name, topic0, parser in [
        ("FundsDeposited",  TOPICS["FundsDeposited"],  parse_deposits),
        ("FundsWithdrawn",  TOPICS["FundsWithdrawn"],  parse_withdrawals),
        ("RewardsIssued",   TOPICS["RewardsIssued"],   parse_rewards_issued),
        ("RewardsWithdrawn", TOPICS["RewardsWithdrawn"], parse_rewards_withdrawn),
    ]:
        print(f"  Fetching {event_name} logs...")
        logs = fetch_logs(topic0)
        print(f"    → {len(logs):,} events")
        all_rows.extend(parser(logs))
        time.sleep(0.25)

    if not all_rows:
        sys.exit("No events found — check API key and contract address.")

    # 2. Build DataFrame
    df = pd.DataFrame(all_rows)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df.sort_values("block", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # 3. Cumulative net staked
    df["cumulative_net_staked"] = df["net_flow"].cumsum()

    # 4. Export CSV
    csv_cols = [
        "datetime", "block", "tx_hash", "event", "position_id",
        "amount", "stake", "net_flow", "cumulative_net_staked",
    ]
    df[csv_cols].to_csv(OUTPUT_CSV, index=False)
    print(f"\nCSV saved → {OUTPUT_CSV}  ({len(df):,} rows)")

    # 5. Summary
    deposits_df   = df[df["event"] == "FundsDeposited"]
    withdrawals_df = df[df["event"] == "FundsWithdrawn"]
    rewards_i_df  = df[df["event"] == "RewardsIssued"]
    rewards_w_df  = df[df["event"] == "RewardsWithdrawn"]

    total_deposited  = deposits_df["amount"].sum()
    total_withdrawn  = withdrawals_df["amount"].sum()
    total_rewards_in = rewards_i_df["amount"].sum()
    total_rewards_out = rewards_w_df["amount"].sum()
    net_staked = df["cumulative_net_staked"].iloc[-1]

    print(f"\n{'─' * 50}")
    print(f"  Total deposited (staked)  : {total_deposited:>14,.2f} ENSO")
    print(f"  Total withdrawn (unstaked): {total_withdrawn:>14,.2f} ENSO")
    print(f"  Net currently staked      : {net_staked:>14,.2f} ENSO")
    print(f"  Total rewards issued      : {total_rewards_in:>14,.2f} ENSO")
    print(f"  Total rewards withdrawn   : {total_rewards_out:>14,.2f} ENSO")
    print(f"  # deposits                : {len(deposits_df):>10,}")
    print(f"  # withdrawals             : {len(withdrawals_df):>10,}")
    print(f"{'─' * 50}")

    # 6. Chart
    build_chart(df)


def build_chart(df: pd.DataFrame):
    """Generate a two-panel time-series chart."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True,
                                    gridspec_kw={"height_ratios": [2, 1]})
    fig.suptitle("ENSO Staking — Token Flows Over Time", fontsize=15, weight="bold")

    # ── Panel 1: Cumulative net staked ────────────────────────────────────
    ax1.fill_between(df["datetime"], 0, df["cumulative_net_staked"],
                     alpha=0.3, color="#2563eb")
    ax1.plot(df["datetime"], df["cumulative_net_staked"],
             color="#2563eb", linewidth=1.5, label="Cumulative net staked")
    ax1.set_ylabel("ENSO Tokens", fontsize=11)
    ax1.set_title("Cumulative Net Staked Tokens", fontsize=12)
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    # ── Panel 2: Daily deposit / withdrawal volumes ───────────────────────
    daily = df.copy()
    daily["date"] = daily["datetime"].dt.date

    dep_daily = (daily[daily["event"] == "FundsDeposited"]
                 .groupby("date")["amount"].sum())
    wth_daily = (daily[daily["event"] == "FundsWithdrawn"]
                 .groupby("date")["amount"].sum())

    # Align indexes
    all_dates = sorted(set(dep_daily.index) | set(wth_daily.index))
    dep_daily = dep_daily.reindex(all_dates, fill_value=0)
    wth_daily = wth_daily.reindex(all_dates, fill_value=0)

    dates_dt = [datetime.combine(d, datetime.min.time()) for d in all_dates]

    bar_width = 0.8
    ax2.bar(dates_dt, dep_daily.values, width=bar_width,
            color="#22c55e", alpha=0.8, label="Deposits (staked)")
    ax2.bar(dates_dt, -wth_daily.values, width=bar_width,
            color="#ef4444", alpha=0.8, label="Withdrawals (unstaked)")
    ax2.axhline(0, color="grey", linewidth=0.5)
    ax2.set_ylabel("ENSO Tokens", fontsize=11)
    ax2.set_title("Daily Staking / Unstaking Volume", fontsize=12)
    ax2.legend(loc="upper left")
    ax2.grid(True, alpha=0.3)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    # x-axis formatting
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate(rotation=45)

    plt.tight_layout()
    plt.savefig(OUTPUT_CHART, dpi=150, bbox_inches="tight")
    print(f"Chart saved → {OUTPUT_CHART}")
    plt.close()


if __name__ == "__main__":
    main()
