"""One reconciliation pass, independent of CLI/env so tests can drive it directly."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

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


def reconcile(pool: PoolClient, all_fixtures: list[Fixture], data_dir: Path,
              dry_run: bool = False, max_tx: int = 25, policy: Policy = Policy()) -> RunReport:
    now = pool.now()
    fixtures = [f for f in all_fixtures if now - LOOKBACK_S <= f.start_ts <= now + policy.horizon_s]
    chain, pools = pool.matches([f.match_id for f in fixtures])
    actions = plan(fixtures, chain, now, policy)
    rep = RunReport(planned=len(actions))
    log.info("%d fixtures in window, %d actions planned", len(fixtures), len(actions))

    audit = data_dir / "oracle-log.jsonl"
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
