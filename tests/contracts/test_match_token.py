"""
test_match_token.py: functional tests of the MatchToken contract on a local
in-memory Ethereum chain (eth-tester / py-evm). No real ETH, no network.

Run:  python tests/contracts/test_match_token.py   (also collected by pytest)
Uses the compiled contract in tests/artifacts/compiled.json.
Every check prints PASS/FAIL; the numbers printed here are evidence for the report.
"""
import json
from pathlib import Path

from web3 import Web3, EthereumTesterProvider
from web3.exceptions import ContractLogicError
from eth_tester.exceptions import TransactionFailed

ART = json.loads((Path(__file__).resolve().parents[1] / "artifacts" / "compiled.json").read_text())
E18 = 10**18


def run():
    w3 = Web3(EthereumTesterProvider())
    admin, minter, treasury, alice, bob, club = w3.eth.accounts[:6]

    # ------------------------------------------------------------ deploy
    art = ART["MatchToken"]
    Factory = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])
    tx = Factory.constructor(admin, minter, treasury).transact({"from": admin})
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    token = w3.eth.contract(address=receipt.contractAddress, abi=art["abi"])
    print(f"Deployed at {token.address}, gas used for deployment: {receipt.gasUsed:,}")

    results = []

    def check(name, cond):
        results.append(cond)
        print(("PASS " if cond else "FAIL ") + name)

    def reverts(fn, **tx):
        """True if the transaction is rejected by the contract (revert)."""
        try:
            fn.transact(tx)
            return False
        except (ContractLogicError, TransactionFailed):
            return True

    def bal(a):
        return token.functions.balanceOf(a).call() / E18

    mid = lambda s: Web3.keccak(text=s)  # match / dataset id helper

    # ------------------------------------------------------------ metadata
    check("name/symbol/decimals",
          (token.functions.name().call(), token.functions.symbol().call(), token.functions.decimals().call())
          == ("Match Data Token", "MATCH", 18))
    check("cap = 100M", token.functions.cap().call() == 100_000_000 * E18)
    check("initial supply 55M minted to treasury", bal(treasury) == 55_000_000)
    check("reward room = 45M", token.functions.remainingRewardRoom().call() == 45_000_000 * E18)

    # ------------------------------------------------------------ rewards
    g = token.functions.rewardContributor(alice, mid("match-1"), 100 * E18).transact({"from": minter})
    gas_reward = w3.eth.get_transaction_receipt(g).gasUsed
    check("minter rewards contributor 100 MATCH", bal(alice) == 100)
    check("same match cannot be rewarded twice",
          reverts(token.functions.rewardContributor(alice, mid("match-1"), 100 * E18), **{"from": minter}))
    check("reward above per-match ceiling (500) reverts",
          reverts(token.functions.rewardContributor(alice, mid("match-2"), 501 * E18), **{"from": minter}))
    check("non-minter cannot reward",
          reverts(token.functions.rewardContributor(bob, mid("match-3"), 10 * E18), **{"from": bob}))

    # ------------------------------------------------------------ data payment + burn
    token.functions.transfer(club, 10_000 * E18).transact({"from": treasury})
    supply_before = token.functions.totalSupply().call()
    tre_before = bal(treasury)
    g = token.functions.payForData(mid("dataset-league-2026"), 1_000 * E18).transact({"from": club})
    gas_pay = w3.eth.get_transaction_receipt(g).gasUsed
    burned = (supply_before - token.functions.totalSupply().call()) / E18
    check("payForData burns 30% (300 of 1000)", burned == 300)
    check("payForData sends 70% to treasury", bal(treasury) - tre_before == 700)

    # ------------------------------------------------------------ holder burn
    token.functions.burn(10 * E18).transact({"from": alice})
    check("holder can burn own tokens", bal(alice) == 90)

    # ------------------------------------------------------------ anti-whale
    limit = token.functions.maxWalletAmount().call() / E18
    check("wallet limit = 2,000,000 MATCH (2%)", limit == 2_000_000)
    check("transfer pushing bob over limit reverts",
          reverts(token.functions.transfer(bob, 2_000_001 * E18), **{"from": treasury}))
    token.functions.transfer(bob, 2_000_000 * E18).transact({"from": treasury})
    check("transfer exactly to the limit works", bal(bob) == 2_000_000)
    token.functions.setLimitExempt(bob, True).transact({"from": admin})
    token.functions.transfer(bob, 1 * E18).transact({"from": treasury})
    check("exempt address can exceed limit", bal(bob) == 2_000_001)
    token.functions.setLimitExempt(bob, False).transact({"from": admin})

    # ------------------------------------------------------------ governance parameters
    token.functions.setBurnShare(5_000).transact({"from": admin})
    check("admin can change burn share to 50%", token.functions.burnShareBps().call() == 5_000)
    check("burn share above 80% reverts", reverts(token.functions.setBurnShare(8_001), **{"from": admin}))
    check("non-admin cannot change burn share", reverts(token.functions.setBurnShare(1_000), **{"from": alice}))
    check("wallet limit below 0.5% reverts", reverts(token.functions.setMaxWallet(49), **{"from": admin}))

    # ------------------------------------------------------------ votes
    check("votes are 0 before delegation", token.functions.getVotes(alice).call() == 0)
    token.functions.delegate(alice).transact({"from": alice})
    check("self-delegation activates 90 votes", token.functions.getVotes(alice).call() == 90 * E18)

    # ------------------------------------------------------------ pause
    token.functions.pause().transact({"from": admin})
    check("transfers revert while paused", reverts(token.functions.transfer(bob, 1 * E18), **{"from": alice}))
    check("non-pauser cannot unpause", reverts(token.functions.unpause(), **{"from": alice}))
    token.functions.unpause().transact({"from": admin})
    token.functions.transfer(club, 1 * E18).transact({"from": alice})
    check("transfers work after unpause", bal(alice) == 89)

    # ------------------------------------------------------------ cap
    supply = token.functions.totalSupply().call()
    check("supply + remaining room == cap (burned tokens free room for future rewards)",
          supply + token.functions.remainingRewardRoom().call() == 100_000_000 * E18)
    check("per-match ceiling cannot be raised above 5,000",
          reverts(token.functions.setMaxRewardPerMatch(5_001 * E18), **{"from": admin}))

    # ------------------------------------------------------------ summary
    print(f"\nGas: rewardContributor ≈ {gas_reward:,}, payForData ≈ {gas_pay:,}")
    print(f"{sum(results)}/{len(results)} checks passed")
    return results


def test_match_token():
    """pytest entry point: every check must pass."""
    results = run()
    assert results and all(results)


if __name__ == "__main__":
    assert all(run())
