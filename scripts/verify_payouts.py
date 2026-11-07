"""
Independent payout audit for BettingPool.

Rebuilds every bet from on-chain events, recomputes what each bettor should get
with its own arithmetic (not the contract's), and compares the result with what
the contract says (claimable) or already paid (Claimed events). Also checks that
the 2 % fee was really burned (Transfer to the zero address in the settle tx).

    python scripts/verify_payouts.py            # all games with bets
    python scripts/verify_payouts.py --match 0x...

Read-only: needs no keys. Exit code 1 if anything does not add up.

Why Blockscout for events: public RPC nodes keep only ~1.5 days of logs, which is
not enough to audit a whole matchday. Blockscout serves the full history.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import requests
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
RPC = "https://ethereum-sepolia-rpc.publicnode.com"
EXPLORER_API = "https://eth-sepolia.blockscout.com/api"
POOL = "0x684C429c009088b2Ed69410d8e974aec5b4fa7Af"
TOKEN = "0xb459280925651b0a4ee6b1f71C88C87cc1787512"
BOARD_URL = "https://raw.githubusercontent.com/blav08/extraliga-onchain-prediction-market/main/data/board.json"
ZERO_TOPIC = "0x" + "00" * 32
STATUS = ["NONE", "OPEN", "SETTLED", "CANCELLED"]
SIDE = ["HOME", "AWAY"]
E = 10**18

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))
pool = w3.eth.contract(address=POOL, abi=json.loads((ROOT / "abi" / "BettingPool.json").read_text()))


def topic(sig: str) -> str:
    return "0x" + Web3.keccak(text=sig).hex().removeprefix("0x")


def explorer_logs(address: str, topic0: str, topic1: str | None = None, topic2: str | None = None) -> list[dict]:
    params = {"module": "logs", "action": "getLogs", "fromBlock": "0", "toBlock": "latest",
              "address": address, "topic0": topic0}
    if topic1:
        params.update(topic1=topic1, topic0_1_opr="and")
    if topic2:
        params.update(topic2=topic2, topic0_2_opr="and", topic1_2_opr="and")
    r = requests.get(EXPLORER_API, params=params, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("status") == "0" and body.get("message") != "No logs found":
        raise RuntimeError(f"explorer error: {body}")
    return body.get("result") or []


def decode(event_name: str, raw: dict) -> dict:
    """Turn an explorer log into decoded event args using the pool ABI."""
    log = {
        "address": Web3.to_checksum_address(raw["address"]),
        "topics": [bytes.fromhex(t[2:]) for t in raw["topics"] if t],
        "data": bytes.fromhex(raw["data"][2:]),
        "blockNumber": int(raw["blockNumber"], 16), "transactionHash": bytes.fromhex(raw["transactionHash"][2:]),
        "logIndex": int(raw.get("logIndex") or "0x0", 16), "transactionIndex": int(raw.get("transactionIndex") or "0x0", 16),
        "blockHash": bytes.fromhex(raw["blockHash"][2:]) if raw.get("blockHash") else b"\x00" * 32,
    }
    ev = getattr(pool.events, event_name)().process_log(log)
    return {**ev["args"], "tx": "0x" + raw["transactionHash"][2:]}


@dataclass
class Game:
    match_id: str
    label: str = ""
    stakes: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))  # bettor -> [home, away]
    claimed: dict = field(default_factory=dict)                                # bettor -> amount
    settled: dict | None = None


def fmt(wei: int) -> str:
    return f"{wei / E:,.6f}".rstrip("0").rstrip(".")


def labels() -> dict[str, str]:
    """Human names for match ids, from the oracle's board (local copy first)."""
    try:
        local = ROOT / "data" / "board.json"
        data = json.loads(local.read_text()) if local.exists() else requests.get(BOARD_URL, timeout=20).json()
        return {m["match_id"].lower(): f"{m['home']} vs {m['away']}" for m in data.get("matches", [])}
    except Exception:
        return {}


def expected_payouts(stakes: dict, winner: int, distributable: int) -> dict[str, int]:
    """Independent re-implementation of the parimutuel formula."""
    win_pool = sum(s[winner] for s in stakes.values())
    return {b: (s[winner] * distributable) // win_pool for b, s in stakes.items() if s[winner] > 0}


def audit(only: str | None) -> int:
    names = labels()
    games: dict[str, Game] = {}
    get = lambda mid: games.setdefault(mid, Game(mid, names.get(mid, "")))

    for raw in explorer_logs(POOL, topic("BetPlaced(bytes32,address,uint8,uint256)")):
        a = decode("BetPlaced", raw)
        g = get("0x" + a["matchId"].hex().removeprefix("0x"))
        g.stakes[a["bettor"]][a["side"]] += a["amount"]
    for raw in explorer_logs(POOL, topic("MatchSettled(bytes32,uint8,uint256,uint256)")):
        a = decode("MatchSettled", raw)
        get("0x" + a["matchId"].hex().removeprefix("0x")).settled = a
    for raw in explorer_logs(POOL, topic("Claimed(bytes32,address,uint256)")):
        a = decode("Claimed", raw)
        get("0x" + a["matchId"].hex().removeprefix("0x")).claimed[a["bettor"]] = a["amount"]

    # every burn by the pool = Transfer(pool -> 0x0) on the token
    pool_topic = "0x" + "00" * 12 + POOL[2:].lower()
    burns = {l["transactionHash"].lower(): int(l["data"], 16)
             for l in explorer_logs(TOKEN, topic("Transfer(address,address,uint256)"), pool_topic, ZERO_TOPIC)}

    fee_bps = pool.functions.feeBps().call()
    problems = 0

    def check(ok: bool, text: str):
        nonlocal problems
        problems += not ok
        print(f"  {'OK  ' if ok else 'FAIL'} {text}")

    for mid, g in games.items():
        if only and mid.lower() != only.lower():
            continue
        start, status, winner, home_pool, away_pool = pool.functions.getMatch(mid).call()
        status = STATUS[status]
        print(f"\n{g.label or '(manual test market)'}  [{status}]  {mid[:10]}…")

        total_events = sum(sum(s) for s in g.stakes.values())
        check(total_events == home_pool + away_pool,
              f"pools from events {fmt(total_events)} = on-chain pools {fmt(home_pool)} + {fmt(away_pool)}")

        if status == "SETTLED":
            s = g.settled
            fee = (s["totalPool"] * fee_bps) // 10_000
            check(s["winner"] == winner, f"winner {SIDE[winner]}")
            check(s["burnedFee"] == fee, f"fee {fmt(s['burnedFee'])} = {fee_bps / 100:.0f} % of {fmt(s['totalPool'])}"
                  + ("" if s["burnedFee"] == fee else " (fee parameter may have changed since)"))
            check(burns.get(s["tx"].lower()) == s["burnedFee"], f"fee really burned in settle tx (Transfer to 0x0: {fmt(burns.get(s['tx'].lower(), 0))})")
            dist = s["totalPool"] - s["burnedFee"]
            exp = expected_payouts(g.stakes, winner, dist)
            for b, st in g.stakes.items():
                want = exp.get(b, 0)
                got = g.claimed.get(b, pool.functions.claimable(mid, b).call())
                how = "paid" if b in g.claimed else "claimable"
                check(want == got, f"{b[:8]}… staked {fmt(st[0])}/{fmt(st[1])} (H/A): expected {fmt(want)}, {how} {fmt(got)}")
            dust = dist - sum(exp.values())
            check(0 <= dust < max(len(exp), 1), f"rounding dust left in pool: {dust} wei")

        elif status == "CANCELLED":
            for b, st in g.stakes.items():
                want = st[0] + st[1]
                got = g.claimed.get(b, pool.functions.claimable(mid, b).call())
                check(want == got, f"{b[:8]}… refund expected {fmt(want)}, {'paid' if b in g.claimed else 'claimable'} {fmt(got)}")

        elif status == "OPEN":
            total = home_pool + away_pool
            dist = total - (total * fee_bps) // 10_000
            print(f"  what-if (pool as it is now, {fee_bps / 100:.0f} % burn):")
            for w in (0, 1):
                if [home_pool, away_pool][w] == 0:
                    print(f"    {SIDE[w]} wins: nobody backed them -> game cancelled, everyone refunded")
                    continue
                exp = expected_payouts(g.stakes, w, dist)
                rows = ", ".join(f"{b[:8]}… gets {fmt(v)}" for b, v in exp.items())
                print(f"    {SIDE[w]} wins: {rows}")

    print(f"\n{'All checks passed.' if problems == 0 else f'{problems} check(s) FAILED.'}")
    return 1 if problems else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--match", help="audit a single match id (0x…)")
    sys.exit(audit(ap.parse_args().match))
