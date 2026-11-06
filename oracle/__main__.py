"""
CLI entry point.

  python -m oracle run [--dry-run]     one reconciliation pass (what CI runs)
  python -m oracle leagues extraliga   find the API-Sports league id
  python -m oracle status              show chain state of fixtures around now

Configuration comes from environment variables (see .env.example), so the
same code runs locally and in GitHub Actions without changes.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from web3 import Web3

from .chain import PoolClient
from .planner import Policy
from .runner import LOOKBACK_S, reconcile
from .sources import ApiSportsSource, FileSource, TheSportsDBSource

log = logging.getLogger("oracle")
ROOT = Path(__file__).resolve().parent.parent
LOW_BALANCE_ETH = 0.01


def env(name: str, default: str | None = None, required: bool = True) -> str | None:
    val = os.environ.get(name) or default
    if required and not val:
        sys.exit(f"missing environment variable {name} (see .env.example)")
    return val


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Tiny .env loader (no extra dependency). Real env vars win over the file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split("#", 1)[0].strip())


def make_source():
    """
    SOURCE=thesportsdb  (default) free, current season, max 3 games/day on the free key
    SOURCE=apisports    full schedule, current season needs a paid plan
    SOURCE=file:x.json  offline demo / tests, no network
    """
    spec = os.environ.get("SOURCE") or "thesportsdb"
    if spec.startswith("file:"):
        return FileSource(spec.removeprefix("file:"))
    if spec == "apisports":
        return ApiSportsSource(env("APISPORTS_KEY"), int(env("LEAGUE_ID")), int(env("SEASON")))
    return TheSportsDBSource(env("THESPORTSDB_KEY", "123", required=False),
                             int(env("THESPORTSDB_LEAGUE", "4923", required=False)))


def run_with(source, pool, dry, max_tx):
    lookup = getattr(source, "lookup", None)
    provider = {TheSportsDBSource: "thesportsdb", ApiSportsSource: "apisports-hockey"}.get(type(source))
    return reconcile(pool, source.fixtures(), ROOT / "data", dry_run=dry, max_tx=max_tx,
                     lookup=lookup, provider=provider)


def make_pool(need_key: bool) -> PoolClient:
    w3 = Web3(Web3.HTTPProvider(env("RPC_URL"), request_kwargs={"timeout": 30}))
    key = env("ORACLE_PRIVATE_KEY", required=False)
    hexpart = (key or "").removeprefix("0x")
    if len(hexpart) != 64 or any(c not in "0123456789abcdefABCDEF" for c in hexpart):
        if need_key:
            sys.exit("ORACLE_PRIVATE_KEY is missing or not a 64-hex-char key (still the placeholder?)")
        key = None  # dry runs and status never sign anything
    return PoolClient(w3, env("POOL_ADDRESS"), key,
                      max_fee_gwei=float(env("MAX_FEE_GWEI", "50", required=False)))


def cmd_run(args) -> int:
    dry = args.dry_run or os.environ.get("DRY_RUN") == "1"
    pool = make_pool(need_key=not dry)
    if not dry:
        if not pool.is_oracle():
            log.error("key %s does not hold ORACLE_ROLE on %s", pool.account.address, pool.pool.address)
            return 2
        if pool.eth_balance() < LOW_BALANCE_ETH:
            log.warning("oracle balance low: %.4f ETH, top it up", pool.eth_balance())
    rep = run_with(make_source(), pool, dry, int(env("MAX_TX_PER_RUN", "25", required=False)))
    log.info("done: %s", rep)
    return 1 if rep.failures else 0  # red CI run when a tx failed


def cmd_leagues(args) -> int:
    src = ApiSportsSource(env("APISPORTS_KEY"), 0, 0)
    for lg in src.leagues(args.search):
        seasons = [s.get("season") for s in lg.get("seasons", [])]
        print(f"id={lg['id']:<6} {lg['name']:<30} {lg.get('country', {}).get('name', '')}  seasons={seasons[-3:]}")
    return 0


def cmd_status(args) -> int:
    pool = make_pool(need_key=False)
    now = pool.now()
    fixtures = [f for f in make_source().fixtures()
                if now - LOOKBACK_S <= f.start_ts <= now + Policy().horizon_s]
    chain, pools = pool.matches([f.match_id for f in fixtures])
    for f in sorted(fixtures, key=lambda x: x.start_ts):
        h, a = (Web3.from_wei(x, "ether") for x in pools[f.match_id])
        print(f"{f.status_code:4} {chain[f.match_id].status.name:9} {f.home} vs {f.away}  pools {h}/{a} MATCH")
    return 0


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="oracle")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="plan only, send nothing")
    r.set_defaults(fn=cmd_run)
    lg = sub.add_parser("leagues")
    lg.add_argument("search", nargs="?", default="extraliga")
    lg.set_defaults(fn=cmd_leagues)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
