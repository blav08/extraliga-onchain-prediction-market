"""
Reconciliation planner: the brain of the oracle, with zero side effects.

Idea (borrowed from Kubernetes controllers): every run we compare
  desired state  = what the real world says (fixtures from the data source)
  actual state   = what the blockchain says (match status in BettingPool)
and emit the minimal list of actions that moves the chain toward reality.

Because the chain itself is the only state, the oracle is stateless and
idempotent: a crashed or skipped run is harmless, the next run simply
re-derives the same plan. No database, nothing to corrupt.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum

from .sources import Fixture, Phase


class ChainStatus(IntEnum):
    """Mirror of BettingPool.Status."""
    NONE = 0
    OPEN = 1
    SETTLED = 2
    CANCELLED = 3


@dataclass(frozen=True)
class ChainMatch:
    status: ChainStatus
    start_ts: int


class Kind(str, Enum):
    CREATE = "create"
    SETTLE = "settle"
    CANCEL = "cancel"
    WARN = "warn"   # needs a human, no transaction


@dataclass(frozen=True)
class Action:
    kind: Kind
    fixture: Fixture
    reason: str
    winner: int | None = None
    start_ts: int | None = None


@dataclass(frozen=True)
class Policy:
    # Only list matches starting within this window (keeps the board tidy)
    horizon_s: int = 7 * 24 * 3600
    # Don't open a market that closes too soon to be worth betting on
    min_lead_s: int = 15 * 60
    # If the real start moved EARLIER than the on-chain close by more than this,
    # people could bet while the game is already running -> void the market
    early_start_tolerance_s: int = 5 * 60


def plan(fixtures: list[Fixture], chain: dict[bytes, ChainMatch], now: int,
         policy: Policy = Policy()) -> list[Action]:
    actions: list[Action] = []
    for f in sorted(fixtures, key=lambda x: x.start_ts):
        cm = chain.get(f.match_id, ChainMatch(ChainStatus.NONE, 0))
        a = _decide(f, cm, now, policy)
        if a:
            actions.append(a)
    # Settle and cancel first: they release user funds; creating new markets can wait
    order = {Kind.SETTLE: 0, Kind.CANCEL: 1, Kind.CREATE: 2, Kind.WARN: 3}
    return sorted(actions, key=lambda a: order[a.kind])


def _decide(f: Fixture, cm: ChainMatch, now: int, p: Policy) -> Action | None:
    if cm.status in (ChainStatus.SETTLED, ChainStatus.CANCELLED):
        return None  # terminal, nothing to do ever again

    if cm.status is ChainStatus.NONE:
        if (f.phase is Phase.SCHEDULED
                and now + p.min_lead_s <= f.start_ts <= now + p.horizon_s):
            return Action(Kind.CREATE, f, "scheduled within horizon", start_ts=f.start_ts)
        return None

    # --- market is OPEN on chain ---------------------------------------
    if f.phase is Phase.VOID:
        return Action(Kind.CANCEL, f, f"provider status {f.status_code}")

    # Integrity guard (the classic way betting contracts get drained):
    # if the real game starts before on-chain betting closes, someone can bet
    # while already watching the game. Any such market is voided, whatever its phase.
    if f.start_ts < cm.start_ts - p.early_start_tolerance_s:
        return Action(Kind.CANCEL, f, "kick-off earlier than on-chain betting close")
    if f.phase in (Phase.LIVE, Phase.FINAL) and now < cm.start_ts:
        return Action(Kind.CANCEL, f, "game in play while betting still open")

    if f.phase is Phase.FINAL:
        if now < cm.start_ts:
            return None  # contract would revert with TooEarly; try next run
        w = f.winner()
        if w is None:
            return Action(Kind.WARN, f, f"final but no clear winner (score {f.score_home}-{f.score_away}, SO {f.shootout})")
        return Action(Kind.SETTLE, f, f"final {f.score_home}-{f.score_away} ({f.status_code})", winner=w)

    return None  # scheduled / live / uncertain -> wait
