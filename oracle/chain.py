"""
Thin, careful wrapper around the deployed BettingPool contract.

Safety rails for an unattended bot that holds a private key:
  * preflight: every tx is first simulated with eth_call; a revert costs nothing
  * explicit nonce management, so several txs in one run never collide
  * EIP-1559 fee cap: the bot refuses to pay more than MAX_FEE_GWEI
  * low-balance warning before the oracle silently runs dry
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from web3 import Web3
from web3.exceptions import ContractLogicError

from .planner import ChainMatch, ChainStatus

log = logging.getLogger("oracle.chain")
ABI = json.loads((Path(__file__).resolve().parent.parent / "abi" / "BettingPool.json").read_text())


@dataclass
class TxResult:
    ok: bool
    tx_hash: str | None = None
    error: str | None = None
    gas_used: int | None = None


class PoolClient:
    def __init__(self, w3: Web3, pool_address: str, private_key: str | None = None,
                 max_fee_gwei: float = 50.0, receipt_timeout: int = 180):
        self.w3 = w3
        self.pool = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=ABI)
        self.account = w3.eth.account.from_key(private_key) if private_key else None
        self.max_fee = Web3.to_wei(max_fee_gwei, "gwei")
        self.receipt_timeout = receipt_timeout
        self._nonce: int | None = None

    # ---------------------------------------------------------------- reads
    def matches(self, match_ids: list[bytes]) -> tuple[dict[bytes, ChainMatch], dict[bytes, tuple[int, int]]]:
        """Chain state + pool sizes for each match id (one getMatch call each)."""
        state, pools = {}, {}
        for mid in match_ids:
            start, status, _winner, home_pool, away_pool = self.pool.functions.getMatch(mid).call()
            state[mid] = ChainMatch(ChainStatus(status), int(start))
            pools[mid] = (int(home_pool), int(away_pool))
        return state, pools

    def now(self) -> int:
        # Chain time, not wall-clock time: that is what the contract compares against
        return int(self.w3.eth.get_block("latest")["timestamp"])

    def is_oracle(self) -> bool:
        role = self.pool.functions.ORACLE_ROLE().call()
        return self.account is not None and self.pool.functions.hasRole(role, self.account.address).call()

    def eth_balance(self) -> float:
        return float(Web3.from_wei(self.w3.eth.get_balance(self.account.address), "ether"))

    # --------------------------------------------------------------- writes
    def create(self, mid: bytes, start_ts: int) -> TxResult:
        return self._send(self.pool.functions.createMatch(mid, start_ts))

    def settle(self, mid: bytes, winner: int) -> TxResult:
        return self._send(self.pool.functions.settle(mid, winner))

    def cancel(self, mid: bytes) -> TxResult:
        return self._send(self.pool.functions.cancel(mid))

    def _send(self, fn) -> TxResult:
        if self.account is None:
            return TxResult(False, error="no private key configured")
        sender = self.account.address

        # 1) Preflight simulation: catches reverts (wrong role, TooEarly, ...) for free
        try:
            fn.call({"from": sender})
        except ContractLogicError as e:
            return TxResult(False, error=f"preflight revert: {e}")
        except Exception as e:  # node-side rejection (no funds, RPC hiccup): still don't send
            return TxResult(False, error=f"preflight failed: {type(e).__name__}: {e}")

        # 2) Fees: tip + 2x base fee headroom, but never above our hard cap
        base = self.w3.eth.get_block("latest").get("baseFeePerGas", 0)
        tip = Web3.to_wei(1.5, "gwei")
        max_fee = min(2 * base + tip, self.max_fee)
        if max_fee < base + tip:
            return TxResult(False, error=f"gas too expensive (base {Web3.from_wei(base, 'gwei')} gwei)")

        if self._nonce is None:
            self._nonce = self.w3.eth.get_transaction_count(sender, "pending")

        tx = fn.build_transaction({
            "from": sender,
            "nonce": self._nonce,
            "maxPriorityFeePerGas": tip,
            "maxFeePerGas": max_fee,
            "chainId": self.w3.eth.chain_id,
        })
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)  # 20 % safety margin

        signed = self.account.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self._nonce += 1
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=self.receipt_timeout)
        tx_hash = "0x" + h.hex().removeprefix("0x")
        if receipt["status"] != 1:
            return TxResult(False, tx_hash, "mined but reverted", receipt["gasUsed"])
        return TxResult(True, tx_hash, gas_used=receipt["gasUsed"])
