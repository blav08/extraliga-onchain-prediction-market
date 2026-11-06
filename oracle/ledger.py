"""
Public outputs of every run, committed back to the repository by CI.

  data/board.json        snapshot the frontend reads (matches, teams, pools, status)
  data/oracle-log.jsonl  append-only audit log: every decision + the evidence behind it

The audit log is the oracle's answer to "why should I trust you?": git history
makes it tamper-evident (rewriting it would show up in every clone), and each
settlement line contains the raw provider data and the tx hash, so anyone can
check the on-chain result against what the data source actually said.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .planner import Action, ChainMatch, ChainStatus
from .sources import Fixture


def _hex(b: bytes) -> str:
    return "0x" + b.hex().removeprefix("0x")


def append_log(path: Path, action: Action, result: dict, dry_run: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": int(time.time()),
        "action": action.kind.value,
        "dry_run": dry_run,
        "match_id": _hex(action.fixture.match_id),
        "match": f"{action.fixture.home} vs {action.fixture.away}",
        "reason": action.reason,
        "winner": {0: "HOME", 1: "AWAY"}.get(action.winner),
        "evidence": action.fixture.evidence(),
        **result,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_board(path: Path, fixtures: list[Fixture], chain: dict[bytes, ChainMatch],
                pools: dict[bytes, tuple[int, int]], meta: dict) -> None:
    rows = []
    for f in sorted(fixtures, key=lambda x: x.start_ts):
        cm = chain.get(f.match_id)
        if cm is None or cm.status is ChainStatus.NONE:
            continue  # only show markets that exist on chain
        home_pool, away_pool = pools.get(f.match_id, (0, 0))
        row = f.public()
        row.update({
            "chain_status": cm.status.name,
            "betting_closes": cm.start_ts,
            "pool_home": str(home_pool),   # strings: JS numbers can't hold 18-decimal wei
            "pool_away": str(away_pool),
        })
        rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"meta": meta, "matches": rows}, ensure_ascii=False, indent=1))
