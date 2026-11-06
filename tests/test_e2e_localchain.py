"""
End-to-end: the real oracle code drives the real contracts on an in-memory EVM.
A scripted "season" plays out across several oracle runs, exactly like CI would.
"""
import json
from pathlib import Path

import pytest
from eth_account import Account
from web3 import EthereumTesterProvider, Web3

from oracle.chain import PoolClient
from oracle.runner import reconcile
from oracle.sources import normalize_apisports
from tests.helpers import game

ART = json.loads((Path(__file__).parent / "artifacts" / "compiled.json").read_text())
E = 10**18


@pytest.fixture
def env(tmp_path):
    provider = EthereumTesterProvider()
    w3 = Web3(provider)
    admin, alice, bob = w3.eth.accounts[:3]

    def deploy(name, *args):
        c = w3.eth.contract(abi=ART[name]["abi"], bytecode=ART[name]["bytecode"])
        addr = w3.eth.get_transaction_receipt(c.constructor(*args).transact({"from": admin})).contractAddress
        return w3.eth.contract(address=addr, abi=ART[name]["abi"])

    # The oracle signs with its own key, like in production
    oracle = Account.create()
    w3.eth.send_transaction({"from": admin, "to": oracle.address, "value": 10 * E})

    token = deploy("MatchToken", admin, admin, admin)
    pool = deploy("BettingPool", token.address, admin, oracle.address)
    token.functions.setLimitExempt(pool.address, True).transact({"from": admin})
    for u in (alice, bob):
        token.functions.transfer(u, 1000 * E).transact({"from": admin})
        token.functions.approve(pool.address, 2**256 - 1).transact({"from": u})

    client = PoolClient(w3, pool.address, oracle.key.hex(), max_fee_gwei=500)

    def travel(seconds):
        provider.ethereum_tester.time_travel(client.now() + seconds)
        provider.ethereum_tester.mine_blocks(1)

    return dict(w3=w3, token=token, pool=pool, client=client, alice=alice, bob=bob,
                travel=travel, data=tmp_path)


def run(env, games):
    fixtures = [normalize_apisports(g) for g in games]
    return reconcile(env["client"], fixtures, env["data"])


def test_full_matchday(env):
    c, pool, token = env["client"], env["pool"], env["token"]
    t0 = c.now()
    kickoff = t0 + 5 * 3600

    # --- run 1: two upcoming games appear -> two markets created
    rep = run(env, [game(100, kickoff), game(200, kickoff)])
    assert (rep.sent, rep.failures) == (2, 0)
    m100 = normalize_apisports(game(100, kickoff)).match_id
    m200 = normalize_apisports(game(200, kickoff)).match_id
    assert pool.functions.getMatch(m100).call()[1] == 1  # OPEN

    # --- run 2: nothing changed -> nothing to do (idempotency)
    assert run(env, [game(100, kickoff), game(200, kickoff)]).sent == 0

    # --- users bet on game 100
    pool.functions.placeBet(m100, 0, 30 * E).transact({"from": env["alice"]})  # HOME
    pool.functions.placeBet(m100, 1, 10 * E).transact({"from": env["bob"]})    # AWAY
    pool.functions.placeBet(m200, 0, 5 * E).transact({"from": env["bob"]})

    # --- game 200 gets postponed before kick-off -> market cancelled
    rep = run(env, [game(100, kickoff), game(200, kickoff + 86400, "POST")])
    assert rep.sent == 1
    assert pool.functions.getMatch(m200).call()[1] == 3  # CANCELLED

    # --- game 100 goes to a shootout, home wins it
    env["travel"](6 * 3600)
    supply = token.functions.totalSupply().call()
    rep = run(env, [game(100, kickoff, "AP", 2, 2, "3-1")])
    assert (rep.sent, rep.failures) == (1, 0)
    _, status, winner, *_ = pool.functions.getMatch(m100).call()
    assert (status, winner) == (2, 0)                                   # SETTLED, HOME
    assert supply - token.functions.totalSupply().call() == 40 * E * 2 // 100  # 2 % burned

    # Alice takes the whole pool minus fee, Bob gets his game-200 refund
    assert pool.functions.claimable(m100, env["alice"]).call() == 392 * E // 10
    assert pool.functions.claimable(m200, env["bob"]).call() == 5 * E

    # --- outputs for the frontend and the audit trail
    board = json.loads((env["data"] / "board.json").read_text())
    assert {m["fixture_id"]: m["chain_status"] for m in board["matches"]} == {100: "SETTLED"}
    log = [json.loads(line) for line in (env["data"] / "oracle-log.jsonl").read_text().splitlines()]
    assert [e["action"] for e in log] == ["create", "create", "cancel", "settle"]
    assert log[-1]["evidence"]["shootout"] == "3-1" and log[-1]["tx_hash"].startswith("0x")


def test_oracle_cannot_act_without_role(env):
    # A random key must fail in preflight, costing no gas and sending nothing
    stranger = PoolClient(env["w3"], env["pool"].address, Account.create().key.hex())
    res = stranger.create(b"\x01" * 32, stranger.now() + 3600)
    assert not res.ok and "preflight" in res.error and res.tx_hash is None
