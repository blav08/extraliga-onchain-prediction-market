"""One reconciliation pass, independent of CLI/env so tests can drive it directly."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .chain import PoolClient
from .ledger import append_log, write_board
from .planner import Kind, Policy, plan
from .sources import Fixture, Phase

log = logging.getLogger("oracle")

# Only look at fixtures near "now": keeps RPC reads small (~2 weeks of games)
LOOKBACK_S = 7 * 24 * 3600

# Settled / cancelled games stay on the board this long, so winners and refunded
# bettors still see a "Collect" button after the data source stops listing the game.
# The contract itself has no claim deadline; this only limits what the web app lists.
RESULTS_KEEP_S = 30 * 24 * 3600


@dataclass
class RunReport:
    planned: int = 0
    sent: int = 0
    failures: int = 0
    warnings: int = 0


def open_markets_from_log(audit: Path, provider: str) -> set[int]:
    """
    Fixture ids of markets this oracle created and has not closed yet, read back
    from its own audit log. Lets sources with partial schedules (free tiers)
    still re-check every market we are responsible for.
    """
    created, closed = set(), set()
    if not audit.exists():
        return set()
    for line in audit.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        ev = e.get("evidence", {})
        if e.get("dry_run") or not e.get("ok") or ev.get("provider") != provider:
            continue
        (created if e["action"] == "create" else closed).add(ev["fixture_id"])
    return created - closed


def closed_markets_from_log(audit: Path, now: int, keep_s: int = RESULTS_KEEP_S,
                            previous_board: Path | None = None) -> list[Fixture]:
    """
    Games this oracle settled or cancelled within the last `keep_s` seconds,
    rebuilt from its own audit log. The log keeps the evidence (score, status,
    start time) and the team names, so the board can still show these games
    after the provider's schedule no longer returns them. Team logos are not in
    the log, so they are carried over from the previous board when available.
    """
    if not audit.exists():
        return []
    logos: dict[str, tuple[str | None, str | None]] = {}
    if previous_board and previous_board.exists():
        try:
            for m in json.loads(previous_board.read_text(encoding="utf-8")).get("matches", []):
                logos[m["match_id"].lower()] = (m.get("home_logo"), m.get("away_logo"))
        except (ValueError, KeyError):
            pass  # a broken old board only costs us logos, never the run

    latest: dict[str, dict] = {}  # match_id -> last successful settle/cancel entry
    for line in audit.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("dry_run") or not e.get("ok") or e.get("action") not in ("settle", "cancel"):
            continue
        latest[e["match_id"].lower()] = e

    out = []
    for mid, e in latest.items():
        ev = e.get("evidence") or {}
        start = ev.get("start_ts")
        if start is None or now - start > keep_s:
            continue  # older than the keep window
        home, _, away = (e.get("match") or "").partition(" vs ")
        score = ev.get("score") or [None, None]
        home_logo, away_logo = logos.get(mid, (None, None))
        out.append(Fixture(
            provider=ev["provider"], fixture_id=int(ev["fixture_id"]), start_ts=int(start),
            status_code=ev.get("status") or "",
            phase=Phase.FINAL if e["action"] == "settle" else Phase.VOID,
            home=home, away=away, home_logo=home_logo, away_logo=away_logo,
            score_home=score[0], score_away=score[1], shootout=ev.get("shootout"),
        ))
    return out


def reconcile(pool: PoolClient, all_fixtures: list[Fixture], data_dir: Path,
              dry_run: bool = False, max_tx: int = 25, policy: Policy = Policy(),
              lookup: Callable[[list[int]], list[Fixture]] | None = None,
              provider: str | None = None) -> RunReport:
    now = pool.now()
    audit = data_dir / "oracle-log.jsonl"
    by_id = {f.fixture_id: f for f in all_fixtures}
    if lookup and provider:
        # Per-game lookups are authoritative and override the schedule listing
        known = sorted(open_markets_from_log(audit, provider))
        for f in lookup(known):
            by_id[f.fixture_id] = f
        if known:
            log.info("re-checked %d open markets via lookup", len(known))
    fixtures = [f for f in by_id.values() if now - LOOKBACK_S <= f.start_ts <= now + policy.horizon_s]
    chain, pools = pool.matches([f.match_id for f in fixtures])
    actions = plan(fixtures, chain, now, policy)
    rep = RunReport(planned=len(actions))
    log.info("%d fixtures in window, %d actions planned", len(fixtures), len(actions))

    for a in actions:
        label = f"{a.kind.value:6} {a.fixture.home} vs {a.fixture.away}: {a.reason}"
        if a.kind is Kind.WARN:
            rep.warnings += 1
            log.warning("NEEDS HUMAN  %s", label)
            append_log(audit, a, {"ok": False}, dry_run)
            continue
        if dry_run:
            log.info("DRY  %s", label)
            continue
        if rep.sent >= max_tx:
            log.info("tx budget reached, rest goes next run")
            break
        if a.kind is Kind.CREATE:
            res = pool.create(a.fixture.match_id, a.start_ts)
        elif a.kind is Kind.SETTLE:
            res = pool.settle(a.fixture.match_id, a.winner)
        else:
            res = pool.cancel(a.fixture.match_id)
        rep.sent += 1
        rep.failures += not res.ok
        log.info("%s %s %s", "OK  " if res.ok else "FAIL", label, res.tx_hash or res.error)
        append_log(audit, a, asdict(res), dry_run)

    if rep.sent:
        chain, pools = pool.matches([f.match_id for f in fixtures])  # board shows fresh state

    # Add recently settled / cancelled games that fell out of the schedule window,
    # so the web app can still offer "Collect" for unclaimed winnings and refunds.
    # They are only shown, never planned: the planner leaves terminal games alone anyway.
    board_path = data_dir / "board.json"
    shown = {f.match_id for f in fixtures}
    results = [f for f in closed_markets_from_log(audit, now, previous_board=board_path)
               if f.match_id not in shown]
    if results:
        extra_chain, extra_pools = pool.matches([f.match_id for f in results])
        # Only games somebody staked on: a settled game with empty pools was
        # auto-cancelled by the contract and would show as a confusing "Void"
        results = [f for f in results if sum(extra_pools[f.match_id]) > 0]
        chain, pools = {**chain, **extra_chain}, {**pools, **extra_pools}
    board_fixtures = fixtures + results
    write_board(board_path, board_fixtures, chain, pools,
                {"generated_at_chain_ts": pool.now(), "pool": pool.pool.address,
                 "chain_id": pool.w3.eth.chain_id})
    return rep
