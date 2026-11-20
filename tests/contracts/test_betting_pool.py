"""
End-to-end tests for BettingPool + MatchFaucet against the real MatchToken,
on a local in-memory EVM (eth-tester). No real ETH, no network.

Run:  python tests/contracts/test_betting_pool.py   (also collected by pytest)
Uses the compiled contracts in tests/artifacts/compiled.json.
"""
import json
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_tester.exceptions import TransactionFailed
from web3 import Web3, EthereumTesterProvider
from web3.exceptions import ContractLogicError

ART = json.loads((Path(__file__).resolve().parents[1] / "artifacts" / "compiled.json").read_text())
E = 10**18  # 1 MATCH in wei units


def run():
    provider = EthereumTesterProvider()
    w3 = Web3(provider)
    admin, oracle, alice, bob, carol = w3.eth.accounts[:5]

    def deploy(name, *args):
        c = ART[name]
        factory = w3.eth.contract(abi=c["abi"], bytecode=c["bytecode"])
        tx = factory.constructor(*args).transact({"from": admin})
        addr = w3.eth.get_transaction_receipt(tx).contractAddress
        return w3.eth.contract(address=addr, abi=c["abi"])

    def now():
        return w3.eth.get_block("latest").timestamp

    def time_travel(seconds):
        # eth-tester lets us move the chain clock forward
        provider.ethereum_tester.time_travel(now() + seconds)
        provider.ethereum_tester.mine_blocks(1)

    def reverts(fn, error_name):
        try:
            fn()
        except (ContractLogicError, TransactionFailed):
            return True  # custom error -> revert as expected
        raise AssertionError(f"expected revert {error_name}")

    passed = []

    def ok(msg):
        passed.append(msg)
        print("PASS", msg)

    # ------------------------------------------------------------ setup
    token = deploy("MatchToken", admin, admin, admin)  # admin = treasury here
    pool = deploy("BettingPool", token.address, admin, oracle)
    faucet = deploy("MatchFaucet", token.address, admin)
    bal = lambda addr: token.functions.balanceOf(addr).call()

    # Pool and faucet hold many users' tokens -> must be exempt from the 2 % wallet limit
    token.functions.setLimitExempt(pool.address, True).transact({"from": admin})
    token.functions.setLimitExempt(faucet.address, True).transact({"from": admin})
    token.functions.transfer(faucet.address, 1_000_000 * E).transact({"from": admin})

    # ------------------------------------------------------------ faucet
    for u in (alice, bob, carol):
        faucet.functions.drip().transact({"from": u})
    assert bal(alice) == 100 * E
    ok("faucet drips 100 MATCH")
    reverts(lambda: faucet.functions.drip().transact({"from": alice}), "CooldownActive")
    ok("faucet cooldown blocks a second claim within 24 h")

    # ------------------------------------------------------------ happy path
    mid = Web3.keccak(text="api-sports:123456")
    start = now() + 3600
    pool.functions.createMatch(mid, start).transact({"from": oracle})
    reverts(lambda: pool.functions.createMatch(mid, start).transact({"from": oracle}), "MatchExists")
    reverts(lambda: pool.functions.createMatch(Web3.keccak(text="x"), start).transact({"from": alice}), "AccessControl")
    ok("only oracle creates matches, no duplicates")

    for u in (alice, bob, carol):
        token.functions.approve(pool.address, 2**256 - 1).transact({"from": u})

    pool.functions.placeBet(mid, 0, 60 * E).transact({"from": alice})  # HOME
    pool.functions.placeBet(mid, 0, 20 * E).transact({"from": bob})    # HOME
    pool.functions.placeBet(mid, 1, 80 * E).transact({"from": carol})  # AWAY
    _, _, _, home, away = pool.functions.getMatch(mid).call()
    assert (home, away) == (80 * E, 80 * E)
    ok("bets recorded in both pools")

    reverts(lambda: pool.functions.placeBet(mid, 0, E // 2).transact({"from": alice}), "BetTooSmall")
    reverts(lambda: pool.functions.settle(mid, 0).transact({"from": oracle}), "TooEarly")
    ok("min bet enforced, cannot settle before start")

    time_travel(3601)
    reverts(lambda: pool.functions.placeBet(mid, 0, 5 * E).transact({"from": alice}), "BettingClosed")
    ok("betting closes at start time")

    supply_before = token.functions.totalSupply().call()
    pool.functions.settle(mid, 0).transact({"from": oracle})  # HOME wins
    burned = supply_before - token.functions.totalSupply().call()
    assert burned == 160 * E * 200 // 10_000  # 2 % of 160 = 3.2 MATCH
    ok(f"settlement burns 2 % fee ({burned / E} MATCH), supply decreases")

    # distributable = 156.8; alice 60/80 -> 117.6, bob 20/80 -> 39.2, carol 0
    assert pool.functions.claimable(mid, alice).call() == 1176 * E // 10
    assert pool.functions.claimable(mid, bob).call() == 392 * E // 10
    assert pool.functions.claimable(mid, carol).call() == 0
    a0 = bal(alice)
    pool.functions.claim(mid).transact({"from": alice})
    assert bal(alice) - a0 == 1176 * E // 10
    reverts(lambda: pool.functions.claim(mid).transact({"from": alice}), "AlreadyClaimed")
    reverts(lambda: pool.functions.claim(mid).transact({"from": carol}), "NothingToClaim")
    pool.functions.claim(mid).transact({"from": bob})
    assert bal(pool.address) == 0
    ok("winners paid pro-rata, double claim and loser claim rejected, pool emptied")

    # ------------------------------------------------------------ nobody backed winner -> refund
    mid2 = Web3.keccak(text="api-sports:2")
    pool.functions.createMatch(mid2, now() + 100).transact({"from": oracle})
    pool.functions.placeBet(mid2, 1, 10 * E).transact({"from": carol})
    time_travel(101)
    pool.functions.settle(mid2, 0).transact({"from": oracle})  # HOME wins, nobody on HOME
    assert pool.functions.getMatch(mid2).call()[1] == 3  # CANCELLED
    c0 = bal(carol)
    pool.functions.claim(mid2).transact({"from": carol})
    assert bal(carol) - c0 == 10 * E
    ok("empty winning side -> auto-cancel and full refund (no fee)")

    # ------------------------------------------------------------ oracle goes silent -> anyone cancels
    mid3 = Web3.keccak(text="api-sports:3")
    pool.functions.createMatch(mid3, now() + 100).transact({"from": oracle})
    pool.functions.placeBet(mid3, 0, 5 * E).transact({"from": alice})
    time_travel(101)
    reverts(lambda: pool.functions.cancel(mid3).transact({"from": bob}), "TooEarly")
    time_travel(3 * 24 * 3600)
    pool.functions.cancel(mid3).transact({"from": bob})
    a0 = bal(alice)
    pool.functions.claim(mid3).transact({"from": alice})
    assert bal(alice) - a0 == 5 * E
    ok("after 3-day timeout anyone can cancel -> refund (oracle liveness fallback)")

    # ------------------------------------------------------------ permit (single-tx bet)
    acct = Account.create()
    w3.eth.send_transaction({"from": admin, "to": acct.address, "value": 10**18})
    token.functions.transfer(acct.address, 50 * E).transact({"from": admin})
    mid4 = Web3.keccak(text="api-sports:4")
    pool.functions.createMatch(mid4, now() + 1000).transact({"from": oracle})

    deadline = now() + 600
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"},
            ],
            "Permit": [
                {"name": "owner", "type": "address"}, {"name": "spender", "type": "address"},
                {"name": "value", "type": "uint256"}, {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "primaryType": "Permit",
        "domain": {"name": "Match Data Token", "version": "1", "chainId": w3.eth.chain_id,
                   "verifyingContract": token.address},
        "message": {"owner": acct.address, "spender": pool.address, "value": 20 * E,
                    "nonce": token.functions.nonces(acct.address).call(), "deadline": deadline},
    }
    sig = Account.sign_message(encode_typed_data(full_message=typed), acct.key)
    tx = pool.functions.placeBetWithPermit(mid4, 0, 20 * E, deadline, sig.v,
                                           sig.r.to_bytes(32, "big"), sig.s.to_bytes(32, "big")
                                           ).build_transaction({"from": acct.address, "nonce": 0,
                                                                "gas": 300_000, "gasPrice": w3.eth.gas_price})
    w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    assert pool.functions.stakesOf(mid4, acct.address).call()[0] == 20 * E
    ok("placeBetWithPermit works without a prior approve tx")

    # ------------------------------------------------------------ pause freezes everything
    token.functions.pause().transact({"from": admin})
    reverts(lambda: pool.functions.placeBet(mid4, 0, 5 * E).transact({"from": alice}), "EnforcedPause")
    token.functions.unpause().transact({"from": admin})
    ok("token pause also freezes betting (expected, worth documenting)")

    print(f"\n{len(passed)} checks passed")
    return passed


def test_betting_pool():
    """pytest entry point: any failed assertion fails the test."""
    assert len(run()) == 12


if __name__ == "__main__":
    run()
