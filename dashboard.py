"""
ENSO Staking Dashboard
======================
Interactive Streamlit dashboard showing staking flows, top stakers,
and position-level detail for the stENSO contract.

Run locally:  streamlit run dashboard.py
"""

import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ENSO Staking Dashboard",
    page_icon="🔒",
    layout="wide",
)

# ── Constants ────────────────────────────────────────────────────────────────
CONTRACT = "0x22Ad2a46d317C5eDF6c01fea16d4399C912E9A01"
DECIMALS = 18
BASE_URL = "https://api.etherscan.io/v2/api"

# Precomputed keccak256 event topic hashes (removes web3 dependency)
TOPIC_POSITION_CREATED  = "0x34e49ed13d7eb52832aff120e7482f7b6e7e0328254ca90ee5834a845a87c3b2"
TOPIC_FUNDS_DEPOSITED   = "0xed2de103da084463a1b2895568d352fd796dfd1d033c0e8ee9fabe73a6715389"
TOPIC_FUNDS_WITHDRAWN   = "0xd66662c0ded9e58fd31d5e44944bcfd07ffc15e6927ecc1382e7941cb7bd24c4"
TOPIC_REWARDS_ISSUED    = "0x0c9657b4fcab07e36b228d7add08afd28c23c3e216910a78c6f12b89d4f05397"
TOPIC_REWARDS_WITHDRAWN = "0x8a43c4352486ec339f487f64af78ca5cbf06cd47833f073d3baf3a193e503161"
TOPIC_TRANSFER          = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_TOPIC = "0x" + "0" * 64


# ── Helpers ──────────────────────────────────────────────────────────────────
def h(val: str) -> int:
    return int(val, 16)


def decode_word(data: str, index: int) -> int:
    start = 2 + index * 64
    return int(data[start : start + 64], 16)


def addr_from_topic(topic: str) -> str:
    return "0x" + topic[-40:]


def format_unlock(expiry_ts: int, now: int) -> str:
    if expiry_ts <= now:
        return "UNLOCKED"
    remaining = expiry_ts - now
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    return f"{days}d {hours}h" if days > 0 else f"{hours}h"


def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}"


def etherscan_link(addr: str) -> str:
    return f"https://etherscan.io/address/{addr}"


def fetch_logs(api_key: str, topic0: str, extra: dict | None = None) -> list[dict]:
    all_logs: list[dict] = []
    from_block = 0
    while True:
        params = {
            "chainid": "1", "module": "logs", "action": "getLogs",
            "address": CONTRACT, "topic0": topic0,
            "fromBlock": from_block, "toBlock": "latest", "apikey": api_key,
        }
        if extra:
            params.update(extra)
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


# ── Data loading (cached) ───────────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def load_data(api_key: str):
    """Fetch all on-chain data and return processed DataFrames."""
    now = int(datetime.now(timezone.utc).timestamp())

    # ── Events ───────────────────────────────────────────────────────────
    # Staking flow events
    dep_logs = fetch_logs(api_key, TOPIC_FUNDS_DEPOSITED)
    wth_logs = fetch_logs(api_key, TOPIC_FUNDS_WITHDRAWN)
    rew_i_logs = fetch_logs(api_key, TOPIC_REWARDS_ISSUED)
    rew_w_logs = fetch_logs(api_key, TOPIC_REWARDS_WITHDRAWN)

    # Position & ownership events
    pc_logs = fetch_logs(api_key, TOPIC_POSITION_CREATED)
    tr_logs = fetch_logs(api_key, TOPIC_TRANSFER)

    # ── Flow DataFrame ───────────────────────────────────────────────────
    flow_rows = []
    for log in dep_logs:
        funds = decode_word(log["data"], 0) / 10**DECIMALS
        flow_rows.append({
            "block": h(log["blockNumber"]),
            "timestamp": h(log["timeStamp"]),
            "event": "Deposit",
            "amount": funds,
            "net_flow": funds,
        })
    for log in wth_logs:
        funds = decode_word(log["data"], 0) / 10**DECIMALS
        flow_rows.append({
            "block": h(log["blockNumber"]),
            "timestamp": h(log["timeStamp"]),
            "event": "Withdrawal",
            "amount": funds,
            "net_flow": -funds,
        })
    for log in rew_i_logs:
        amt = decode_word(log["data"], 0) / 10**DECIMALS
        flow_rows.append({
            "block": h(log["blockNumber"]),
            "timestamp": h(log["timeStamp"]),
            "event": "Rewards Issued",
            "amount": amt,
            "net_flow": 0.0,
        })
    for log in rew_w_logs:
        amt = decode_word(log["data"], 0) / 10**DECIMALS
        flow_rows.append({
            "block": h(log["blockNumber"]),
            "timestamp": h(log["timeStamp"]),
            "event": "Rewards Withdrawn",
            "amount": amt,
            "net_flow": 0.0,
        })

    flow_df = pd.DataFrame(flow_rows)
    flow_df["datetime"] = pd.to_datetime(flow_df["timestamp"], unit="s", utc=True)
    flow_df.sort_values("block", inplace=True)
    flow_df.reset_index(drop=True, inplace=True)
    flow_df["cumulative_net_staked"] = flow_df["net_flow"].cumsum()

    # ── Positions ────────────────────────────────────────────────────────
    positions: dict[int, dict] = {}
    for log in pc_logs:
        pid = h(log["topics"][1])
        expiry = h(log["data"][2:66])
        validator = bytes.fromhex(log["topics"][2][2:]).rstrip(b"\x00").decode("utf-8", errors="replace")
        positions[pid] = {
            "position_id": pid, "expiry_ts": expiry, "validator": validator,
            "owner": None, "net_deposited": 0.0, "stake": 0.0,
        }

    for log in tr_logs:
        to_addr = addr_from_topic(log["topics"][2])
        token_id = h(log["topics"][3])
        if token_id in positions:
            positions[token_id]["owner"] = to_addr

    for log in dep_logs:
        pid = h(log["topics"][1])
        if pid in positions:
            positions[pid]["net_deposited"] += decode_word(log["data"], 0) / 10**DECIMALS
            positions[pid]["stake"] += decode_word(log["data"], 1) / 10**DECIMALS

    for log in wth_logs:
        pid = h(log["topics"][1])
        if pid in positions:
            positions[pid]["net_deposited"] -= decode_word(log["data"], 0) / 10**DECIMALS

    pos_df = pd.DataFrame(positions.values())
    pos_df["expiry_utc"] = pd.to_datetime(pos_df["expiry_ts"], unit="s", utc=True)
    pos_df["unlock_remaining"] = pos_df["expiry_ts"].apply(lambda t: format_unlock(t, now))
    pos_df["is_locked"] = pos_df["expiry_ts"] > now

    active_df = pos_df[pos_df["net_deposited"] > 0].copy()

    # ── Owner aggregation ────────────────────────────────────────────────
    owner_df = (
        active_df.groupby("owner")
        .agg(
            total_staked=("net_deposited", "sum"),
            total_stake_weight=("stake", "sum"),
            num_positions=("position_id", "count"),
            earliest_unlock=("expiry_ts", "min"),
            latest_unlock=("expiry_ts", "max"),
        )
        .reset_index()
        .sort_values("total_staked", ascending=False)
        .reset_index(drop=True)
    )
    owner_df["earliest_unlock_utc"] = pd.to_datetime(owner_df["earliest_unlock"], unit="s", utc=True)
    owner_df["latest_unlock_utc"] = pd.to_datetime(owner_df["latest_unlock"], unit="s", utc=True)
    owner_df["earliest_remaining"] = owner_df["earliest_unlock"].apply(lambda t: format_unlock(t, now))
    owner_df["latest_remaining"] = owner_df["latest_unlock"].apply(lambda t: format_unlock(t, now))
    owner_df["rank"] = range(1, len(owner_df) + 1)

    return flow_df, active_df, owner_df, now


# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("ENSO Staking")
    st.caption(f"Contract: `{short_addr(CONTRACT)}`")
    st.markdown(f"[View on Etherscan]({etherscan_link(CONTRACT)})")
    st.divider()

    # Support both Streamlit Cloud secrets and local .env
    api_key = st.secrets.get("ETHERSCAN_API_KEY", "") if hasattr(st, "secrets") else ""
    if not api_key:
        api_key = os.getenv("ETHERSCAN_API_KEY", "")
    api_key_input = st.text_input(
        "Etherscan API Key",
        value=api_key,
        type="password",
        help="Required to fetch on-chain data. Get one free at etherscan.io.",
    )
    if not api_key_input:
        st.warning("Enter your Etherscan API key to load data.")
        st.stop()

    refresh = st.button("🔄 Refresh data", use_container_width=True)
    if refresh:
        st.cache_data.clear()

# ── Load data ────────────────────────────────────────────────────────────────
with st.spinner("Fetching on-chain data..."):
    flow_df, pos_df, owner_df, now_ts = load_data(api_key_input)

now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)

# ── KPI row ──────────────────────────────────────────────────────────────────
st.markdown("## ENSO Staking Dashboard")
st.caption(f"Data as of {now_dt.strftime('%Y-%m-%d %H:%M UTC')}")

total_staked = owner_df["total_staked"].sum()
total_deposited = flow_df[flow_df["event"] == "Deposit"]["amount"].sum()
total_withdrawn = flow_df[flow_df["event"] == "Withdrawal"]["amount"].sum()
total_rewards = flow_df[flow_df["event"] == "Rewards Issued"]["amount"].sum()
num_stakers = len(owner_df)
num_positions = len(pos_df)
locked_pct = pos_df["is_locked"].sum() / len(pos_df) * 100 if len(pos_df) > 0 else 0

k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Net Staked", f"{total_staked:,.0f}")
k2.metric("Total Deposited", f"{total_deposited:,.0f}")
k3.metric("Total Withdrawn", f"{total_withdrawn:,.0f}")
k4.metric("Rewards Issued", f"{total_rewards:,.0f}")
k5.metric("Unique Stakers", f"{num_stakers:,}")
k6.metric("Locked Positions", f"{locked_pct:.0f}%")

st.divider()

# ── Tab layout ───────────────────────────────────────────────────────────────
tab_overview, tab_stakers, tab_positions, tab_unlocks = st.tabs(
    ["📈 Staking Overview", "🏆 Top Stakers", "📋 All Positions", "🔓 Unlock Schedule"]
)

# ── TAB 1: Overview ──────────────────────────────────────────────────────────
with tab_overview:
    # Cumulative staked chart
    fig_cum = go.Figure()
    fig_cum.add_trace(go.Scatter(
        x=flow_df["datetime"], y=flow_df["cumulative_net_staked"],
        fill="tozeroy", fillcolor="rgba(37,99,235,0.15)",
        line=dict(color="#2563eb", width=2),
        name="Cumulative Net Staked",
    ))
    fig_cum.update_traces(hovertemplate="%{x|%Y-%m-%d}<br>%{y:,.0f} ENSO<extra></extra>")
    fig_cum.update_layout(
        title="Cumulative Net Staked ENSO Over Time",
        xaxis_title="Date", yaxis_title="ENSO Tokens",
        yaxis_tickformat=",.", yaxis_separatethousands=True,
        height=420, hovermode="x unified",
    )
    st.plotly_chart(fig_cum, use_container_width=True)

    # Daily volume chart
    daily = flow_df.copy()
    daily["date"] = daily["datetime"].dt.date
    dep_daily = daily[daily["event"] == "Deposit"].groupby("date")["amount"].sum().reset_index()
    dep_daily.columns = ["date", "amount"]
    dep_daily["type"] = "Deposits"
    wth_daily = daily[daily["event"] == "Withdrawal"].groupby("date")["amount"].sum().reset_index()
    wth_daily.columns = ["date", "amount"]
    wth_daily["amount"] = -wth_daily["amount"]
    wth_daily["type"] = "Withdrawals"
    vol_df = pd.concat([dep_daily, wth_daily])

    fig_vol = px.bar(
        vol_df, x="date", y="amount", color="type",
        color_discrete_map={"Deposits": "#22c55e", "Withdrawals": "#ef4444"},
        title="Daily Staking & Unstaking Volume",
        labels={"amount": "ENSO Tokens", "date": "Date"},
    )
    fig_vol.update_traces(hovertemplate="%{x|%Y-%m-%d}<br>%{y:,.0f} ENSO<extra></extra>")
    fig_vol.update_layout(
        yaxis_tickformat=",.", yaxis_separatethousands=True,
        height=350, barmode="relative", hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig_vol, use_container_width=True)

# ── TAB 2: Top Stakers ──────────────────────────────────────────────────────
with tab_stakers:
    col_chart, col_table = st.columns([1, 1])

    with col_chart:
        # Top 10 pie chart
        top10 = owner_df.head(10).copy()
        others = pd.DataFrame([{
            "owner": "Others",
            "total_staked": owner_df.iloc[10:]["total_staked"].sum() if len(owner_df) > 10 else 0,
        }])
        pie_df = pd.concat([top10[["owner", "total_staked"]], others])
        pie_df["label"] = pie_df["owner"].apply(
            lambda x: short_addr(x) if x != "Others" else x
        )
        fig_pie = px.pie(
            pie_df, values="total_staked", names="label",
            title="Staking Distribution (Top 10 + Others)",
            hole=0.4,
        )
        fig_pie.update_traces(textposition="inside", textinfo="percent+label")
        fig_pie.update_layout(height=500, showlegend=False)
        st.plotly_chart(fig_pie, use_container_width=True)

    with col_table:
        st.markdown("#### Top 30 Stakers")
        display_df = owner_df.head(30).copy()
        display_df["share"] = (display_df["total_staked"] / total_staked * 100).round(2)
        display_df["address"] = display_df["owner"].apply(short_addr)
        display_df["total_staked"] = display_df["total_staked"].apply(lambda x: f"{x:,.0f}")

        st.dataframe(
            display_df[[
                "rank", "address", "total_staked", "share",
                "num_positions", "earliest_remaining", "latest_remaining",
            ]].rename(columns={
                "rank": "Rank",
                "address": "Address",
                "total_staked": "Staked (ENSO)",
                "share": "Share %",
                "num_positions": "Positions",
                "earliest_remaining": "Earliest Unlock",
                "latest_remaining": "Latest Unlock",
            }),
            use_container_width=True,
            hide_index=True,
            height=460,
        )

    # Full searchable table
    st.markdown("#### Search Stakers")
    search = st.text_input("Filter by address", placeholder="0x...")
    filtered = owner_df.copy()
    if search:
        filtered = filtered[filtered["owner"].str.lower().str.contains(search.lower())]
    fmt_filtered = filtered.copy()
    fmt_filtered["total_staked"] = fmt_filtered["total_staked"].apply(lambda x: f"{x:,.0f}")
    fmt_filtered["total_stake_weight"] = fmt_filtered["total_stake_weight"].apply(lambda x: f"{x:,.0f}")
    st.dataframe(
        fmt_filtered[[
            "rank", "owner", "total_staked", "total_stake_weight",
            "num_positions", "earliest_remaining", "latest_remaining",
        ]].rename(columns={
            "rank": "Rank", "owner": "Address",
            "total_staked": "Staked (ENSO)",
            "total_stake_weight": "Stake Weight",
            "num_positions": "Positions",
            "earliest_remaining": "Earliest Unlock",
            "latest_remaining": "Latest Unlock",
        }),
        use_container_width=True,
        hide_index=True,
    )

# ── TAB 3: All Positions ────────────────────────────────────────────────────
with tab_positions:
    st.markdown("#### All Active Staking Positions")

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        lock_filter = st.selectbox("Lock status", ["All", "Locked", "Unlocked"])
    with col_f2:
        validator_opts = ["All"] + sorted(pos_df["validator"].unique().tolist())
        val_filter = st.selectbox("Validator", validator_opts)

    show_df = pos_df.copy()
    if lock_filter == "Locked":
        show_df = show_df[show_df["is_locked"]]
    elif lock_filter == "Unlocked":
        show_df = show_df[~show_df["is_locked"]]
    if val_filter != "All":
        show_df = show_df[show_df["validator"] == val_filter]

    show_df = show_df.sort_values("net_deposited", ascending=False)
    show_df["owner_short"] = show_df["owner"].apply(
        lambda x: short_addr(x) if x else "Unknown"
    )
    fmt_show = show_df.copy()
    fmt_show["net_deposited"] = fmt_show["net_deposited"].apply(lambda x: f"{x:,.0f}")
    fmt_show["stake"] = fmt_show["stake"].apply(lambda x: f"{x:,.0f}")

    st.dataframe(
        fmt_show[[
            "position_id", "owner_short", "net_deposited", "stake",
            "validator", "expiry_utc", "unlock_remaining", "is_locked",
        ]].rename(columns={
            "position_id": "Position ID",
            "owner_short": "Owner",
            "net_deposited": "Net Deposited (ENSO)",
            "stake": "Stake Weight",
            "validator": "Validator",
            "expiry_utc": "Expiry (UTC)",
            "unlock_remaining": "Unlock In",
            "is_locked": "Locked",
        }),
        use_container_width=True,
        hide_index=True,
        height=500,
    )

# ── TAB 4: Unlock Schedule ──────────────────────────────────────────────────
with tab_unlocks:
    st.markdown("#### Token Unlock Schedule")
    st.caption("When do staked tokens become withdrawable?")

    locked_df = pos_df[pos_df["is_locked"]].copy()
    if locked_df.empty:
        st.info("No locked positions found.")
    else:
        locked_df["unlock_month"] = locked_df["expiry_utc"].dt.to_period("M").astype(str)
        monthly = (
            locked_df.groupby("unlock_month")
            .agg(tokens_unlocking=("net_deposited", "sum"), positions=("position_id", "count"))
            .reset_index()
        )

        fig_unlock = px.bar(
            monthly, x="unlock_month", y="tokens_unlocking",
            text="positions",
            title="ENSO Tokens Unlocking by Month",
            labels={"unlock_month": "Month", "tokens_unlocking": "ENSO Tokens"},
            color_discrete_sequence=["#8b5cf6"],
        )
        fig_unlock.update_traces(
            texttemplate="%{text} pos", textposition="outside",
            hovertemplate="%{x}<br>%{y:,.0f} ENSO<br>%{text} positions<extra></extra>",
        )
        fig_unlock.update_layout(
            yaxis_tickformat=",.", yaxis_separatethousands=True, height=420,
        )
        st.plotly_chart(fig_unlock, use_container_width=True)

        # Cumulative unlock curve
        unlock_sorted = locked_df.sort_values("expiry_utc")
        unlock_sorted["cumulative_unlocking"] = unlock_sorted["net_deposited"].cumsum()
        already_unlocked = pos_df[~pos_df["is_locked"]]["net_deposited"].sum()

        fig_curve = go.Figure()
        fig_curve.add_trace(go.Scatter(
            x=unlock_sorted["expiry_utc"],
            y=unlock_sorted["cumulative_unlocking"] + already_unlocked,
            fill="tozeroy", fillcolor="rgba(139,92,246,0.15)",
            line=dict(color="#8b5cf6", width=2),
            name="Cumulative Unlocked",
        ))
        fig_curve.add_hline(
            y=total_staked, line_dash="dash", line_color="grey",
            annotation_text=f"Total Staked: {total_staked:,.0f}",
        )
        fig_curve.update_traces(hovertemplate="%{x|%Y-%m-%d}<br>%{y:,.0f} ENSO<extra></extra>")
        fig_curve.update_layout(
            title="Cumulative Token Unlock Curve",
            xaxis_title="Date", yaxis_title="ENSO Tokens (Unlocked)",
            yaxis_tickformat=",.", yaxis_separatethousands=True, height=420,
        )
        st.plotly_chart(fig_curve, use_container_width=True)

# ── Footer ───────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"stENSO contract: [{CONTRACT}]({etherscan_link(CONTRACT)}) · "
    f"Data fetched from Etherscan API · Last refresh: {now_dt.strftime('%Y-%m-%d %H:%M UTC')}"
)
