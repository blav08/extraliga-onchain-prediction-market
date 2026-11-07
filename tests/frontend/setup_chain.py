"""
Frontend E2E, step 1. Needs a local node with Sepolia chain id:
  npx hardhat node  (hardhat.config.js: networks.hardhat.chainId = 11155111)

Deploy the real contracts to the local node and stage three games: open, settled (user won), cancelled."""
import json, time
from pathlib import Path
from web3 import Web3

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ART = json.loads((ROOT / "tests/artifacts/compiled.json").read_text())
w3 = Web3(Web3.HTTPProvider("http://localhost:8545"))
admin, oracle, user, other = w3.eth.accounts[:4]
E = 10**18

def deploy(name, *args):
    c = w3.eth.contract(abi=ART[name]["abi"], bytecode=ART[name]["bytecode"])
    addr = w3.eth.get_transaction_receipt(c.constructor(*args).transact({"from": admin})).contractAddress
    return w3.eth.contract(address=addr, abi=ART[name]["abi"])

token = deploy("MatchToken", admin, admin, admin)
pool = deploy("BettingPool", token.address, admin, oracle)
faucet = deploy("MatchFaucet", token.address, admin)
token.functions.setLimitExempt(pool.address, True).transact({"from": admin})
token.functions.setLimitExempt(faucet.address, True).transact({"from": admin})
token.functions.transfer(faucet.address, 1_000_000 * E).transact({"from": admin})
token.functions.transfer(other, 500 * E).transact({"from": admin})
token.functions.transfer(user, 40 * E).transact({"from": admin})  # user starts with 40, faucet adds 100
token.functions.approve(pool.address, 2**256 - 1).transact({"from": other})
# user allows exactly the two staged bets, so the UI's first bet must ask for a new allowance
token.functions.approve(pool.address, 30 * E).transact({"from": user})

now = w3.eth.get_block("latest").timestamp
mid = lambda fid: Web3.keccak(text=f"thesportsdb:{fid}")
OPEN, WON, VOID = 9001, 9002, 9003

pool.functions.createMatch(mid(OPEN), now + 3 * 86400).transact({"from": oracle})
pool.functions.createMatch(mid(WON), now + 120).transact({"from": oracle})
pool.functions.createMatch(mid(VOID), now + 3600).transact({"from": oracle})

# someone else already bet on the open game, so the odds are not trivial
pool.functions.placeBet(mid(OPEN), 0, 60 * E).transact({"from": other})
pool.functions.placeBet(mid(OPEN), 1, 140 * E).transact({"from": other})
# settled game: user backs home, other backs away, home wins
pool.functions.placeBet(mid(WON), 0, 20 * E).transact({"from": user})
pool.functions.placeBet(mid(WON), 1, 80 * E).transact({"from": other})
# cancelled game: user staked 10, game postponed
pool.functions.placeBet(mid(VOID), 1, 10 * E).transact({"from": user})

w3.provider.make_request("evm_increaseTime", [200]); w3.provider.make_request("evm_mine", [])
pool.functions.settle(mid(WON), 0).transact({"from": oracle})
pool.functions.cancel(mid(VOID)).transact({"from": oracle})

def row(fid, home, away, start, status, sh=None, sa=None, code="NS"):
    return {"provider": "thesportsdb", "fixture_id": fid, "start_ts": start, "status_code": code,
            "home": home, "away": away, "home_logo": None, "away_logo": None,
            "score_home": sh, "score_away": sa, "match_id": "0x" + mid(fid).hex().removeprefix("0x"),
            "chain_status": status, "betting_closes": start, "pool_home": "0", "pool_away": "0"}

board = {"meta": {}, "matches": [
    row(OPEN, "HC Sparta Praha", "HC Kometa Brno", now + 3 * 86400, "OPEN"),
    row(WON, "HC Dynamo Pardubice", "Rytíři Kladno <img src=x onerror=alert(1)>", now + 120, "SETTLED", 3, 2, "AP"),
    row(VOID, "HC Škoda Plzeň", "Mountfield HK", now + 3600, "CANCELLED", code="POST"),
]}
(ROOT / "docs/test_board.json").write_text(json.dumps(board, ensure_ascii=False))
json.dump({"token": token.address, "pool": pool.address, "faucet": faucet.address, "user": user,
           "mid_open": "0x" + mid(OPEN).hex().removeprefix("0x"),
           "mid_won": "0x" + mid(WON).hex().removeprefix("0x"),
           "mid_void": "0x" + mid(VOID).hex().removeprefix("0x")}, (HERE / "env.json").open("w"))
print("deployed; user balance", token.functions.balanceOf(user).call() / E,
      "claimable won", pool.functions.claimable(mid(WON), user).call() / E,
      "claimable void", pool.functions.claimable(mid(VOID), user).call() / E)
