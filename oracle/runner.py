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
from .sources import Fixture

log = logging.getLogger("oracle")

# Only look at fixtures near "now": keeps RPC reads small (~2 weeks of games)
LOOKBACK_S = 7 * 24 * 3600


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
    write_board(data_dir / "board.json", fixtures, chain, pools,
                {"generated_at_chain_ts": pool.now(), "pool": pool.pool.address,
                 "chain_id": pool.w3.eth.chain_id})
    return rep
