"""
Match data sources.

Everything the oracle knows about real-world matches comes through a Source.
The rest of the code only ever sees the normalized `Fixture` dataclass, so
switching providers (API-Sports today, something else tomorrow) means writing
one new Source class, nothing else.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Protocol

import requests
from web3 import Web3


class Phase(str, Enum):
    """Provider-independent lifecycle of a match."""
    SCHEDULED = "scheduled"   # not started yet
    LIVE = "live"             # in play or in a break
    FINAL = "final"           # finished, result is official
    VOID = "void"             # postponed / cancelled / abandoned -> refund bets
    UNCERTAIN = "uncertain"   # interrupted etc. -> wait, do nothing


# API-Sports hockey status codes -> our phases.
# Source: https://api-sports.io/documentation/hockey/v1 (status list, read 2026-11-06)
APISPORTS_PHASE = {
    "NS": Phase.SCHEDULED,
    "P1": Phase.LIVE, "P2": Phase.LIVE, "P3": Phase.LIVE,
    "OT": Phase.LIVE, "PT": Phase.LIVE, "BT": Phase.LIVE,
    "FT": Phase.FINAL, "AOT": Phase.FINAL, "AP": Phase.FINAL, "AW": Phase.FINAL,
    "POST": Phase.VOID, "CANC": Phase.VOID, "ABD": Phase.VOID,
    "INTR": Phase.UNCERTAIN,
}

HOME, AWAY = 0, 1  # must match BettingPool.Side enum order


def match_id_for(provider: str, fixture_id: int | str) -> bytes:
    """
    Deterministic on-chain ID for a real-world fixture.
    Anyone (frontend, auditor) can recompute it from the public fixture id,
    so no lookup table is needed and the same match can never be created twice.
    """
    return Web3.keccak(text=f"{provider}:{fixture_id}")


@dataclass(frozen=True)
class Fixture:
    provider: str
    fixture_id: int
    start_ts: int                  # unix seconds, scheduled start
    status_code: str               # raw provider code, kept for the audit log
    phase: Phase
    home: str
    away: str
    home_logo: str | None = None
    away_logo: str | None = None
    score_home: int | None = None
    score_away: int | None = None
    shootout: str | None = None    # e.g. "3-2", only after a shootout
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def match_id(self) -> bytes:
        return match_id_for(self.provider, self.fixture_id)

    def winner(self) -> int | None:
        """
        HOME / AWAY when the result is final and unambiguous, otherwise None.
        Market = winner including overtime and shootout (a hockey game always
        has a winner). If the score is level we fall back to the shootout line;
        if that is missing too we refuse to guess and return None.
        """
        if self.phase is not Phase.FINAL or self.score_home is None or self.score_away is None:
            return None
        if self.score_home != self.score_away:
            return HOME if self.score_home > self.score_away else AWAY
        so = _parse_pair(self.shootout)
        if so and so[0] != so[1]:
            return HOME if so[0] > so[1] else AWAY
        return None

    def evidence(self) -> dict:
        """Compact proof of what the oracle saw, written to the audit log."""
        return {
            "provider": self.provider,
            "fixture_id": self.fixture_id,
            "status": self.status_code,
            "score": [self.score_home, self.score_away],
            "shootout": self.shootout,
            "start_ts": self.start_ts,
        }

    def public(self) -> dict:
        d = asdict(self)
        d.pop("raw")
        d["phase"] = self.phase.value
        d["match_id"] = "0x" + self.match_id.hex().removeprefix("0x")
        return d


def _parse_pair(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", str(text))
    return (int(m.group(1)), int(m.group(2))) if m else None


class Source(Protocol):
    def fixtures(self) -> list[Fixture]: ...


def normalize_apisports(game: dict) -> Fixture:
    code = (game.get("status") or {}).get("short", "")
    teams = game.get("teams") or {}
    scores = game.get("scores") or {}
    periods = game.get("periods") or {}
    return Fixture(
        provider="apisports-hockey",
        fixture_id=int(game["id"]),
        start_ts=int(game["timestamp"]),
        status_code=code,
        # Unknown codes are treated as UNCERTAIN: never act on data we don't understand
        phase=APISPORTS_PHASE.get(code, Phase.UNCERTAIN),
        home=teams.get("home", {}).get("name", "?"),
        away=teams.get("away", {}).get("name", "?"),
        home_logo=teams.get("home", {}).get("logo"),
        away_logo=teams.get("away", {}).get("logo"),
        score_home=scores.get("home"),
        score_away=scores.get("away"),
        shootout=periods.get("penalties"),
        raw=game,
    )


class ApiSportsSource:
    """
    One HTTP request per run: all games of the league season.
    The free plan allows 100 requests/day, so a run every 30 min (48/day) fits
    with room to spare for manual runs.
    """
    BASE = "https://v1.hockey.api-sports.io"

    def __init__(self, api_key: str, league_id: int, season: int, timeout: int = 20):
        self.session = requests.Session()
        self.session.headers["x-apisports-key"] = api_key
        self.league_id, self.season, self.timeout = league_id, season, timeout

    def _get(self, path: str, **params) -> dict:
        r = self.session.get(f"{self.BASE}/{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        # API-Sports returns HTTP 200 with an "errors" object for auth/quota problems
        errors = body.get("errors")
        if errors:
            raise RuntimeError(f"API-Sports error: {errors}")
        return body

    def fixtures(self) -> list[Fixture]:
        body = self._get("games", league=self.league_id, season=self.season)
        return [normalize_apisports(g) for g in body.get("response", [])]

    def leagues(self, search: str) -> list[dict]:
        body = self._get("leagues", search=search)
        return body.get("response", [])


class FileSource:
    """Reads API-Sports-shaped games from a JSON file. Used for demos and tests."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fixtures(self) -> list[Fixture]:
        data = json.loads(self.path.read_text())
        games = data.get("response", data) if isinstance(data, dict) else data
        return [normalize_apisports(g) for g in games]


# --------------------------------------------------------------------------
# TheSportsDB (free key "123"). Free tier limits found on 2026-11-06:
#   eventsday   -> at most 3 games per day and league
#   lookupevent -> full detail of one game, works on the free key
# So the schedule comes day by day, and every market we have already opened
# is re-checked individually through lookupevent, which is authoritative.
# --------------------------------------------------------------------------

# TheSportsDB mostly uses the same short codes as API-Sports, plus some long forms
THESPORTSDB_PHASE = {
    **APISPORTS_PHASE,
    "Not Started": Phase.SCHEDULED,
    "Match Finished": Phase.FINAL,
    "Postponed": Phase.VOID, "Cancelled": Phase.VOID, "Abandoned": Phase.VOID,
}


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(event: dict) -> int:
    from datetime import datetime, timezone
    raw = event.get("strTimestamp") or f"{event.get('dateEvent')}T{event.get('strTime') or '00:00:00'}"
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:          # TheSportsDB timestamps are UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def normalize_thesportsdb(event: dict) -> Fixture:
    code = (event.get("strStatus") or "").strip()
    phase = THESPORTSDB_PHASE.get(code, Phase.UNCERTAIN)
    if str(event.get("strPostponed", "")).lower() == "yes":
        phase = Phase.VOID
    return Fixture(
        provider="thesportsdb",
        fixture_id=int(event["idEvent"]),
        start_ts=_parse_ts(event),
        status_code=code or "?",
        phase=phase,
        home=event.get("strHomeTeam") or "?",
        away=event.get("strAwayTeam") or "?",
        home_logo=event.get("strHomeTeamBadge"),
        away_logo=event.get("strAwayTeamBadge"),
        score_home=_to_int(event.get("intHomeScore")),
        score_away=_to_int(event.get("intAwayScore")),
        shootout=None,  # not exposed; on this feed the score already includes the shootout winner
        raw=event,
    )


class TheSportsDBSource:
    BASE = "https://www.thesportsdb.com/api/v1/json"

    def __init__(self, api_key: str = "123", league_id: int = 4923,
                 days_back: int = 0, days_ahead: int = 7, min_interval_s: float = 4.0,
                 timeout: int = 20):
        self.base = f"{self.BASE}/{api_key}"
        self.league_id = league_id
        self.days_back, self.days_ahead = days_back, days_ahead
        self.min_interval_s = min_interval_s   # free tier rate limit is strict; 429s are retried
        self.timeout = timeout
        self.session = requests.Session()
        self._last = 0.0

    def _get(self, endpoint: str, retries: int = 4, **params) -> list[dict]:
        import time
        for attempt in range(retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            r = self.session.get(f"{self.base}/{endpoint}", params=params, timeout=self.timeout)
            self._last = time.monotonic()
            if r.status_code == 429 and attempt < retries:
                # Rate limited: honour Retry-After if given, else back off exponentially
                delay = float(r.headers.get("Retry-After") or 15 * 2 ** attempt)
                logging.getLogger("oracle.source").warning("rate limited, retrying in %.0f s", delay)
                time.sleep(min(delay, 60))
                continue
            r.raise_for_status()
            return (r.json() or {}).get("events") or []
        return []  # unreachable, keeps type checkers happy

    def fixtures(self) -> list[Fixture]:
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc).date()
        out: dict[int, Fixture] = {}
        for delta in range(-self.days_back, self.days_ahead + 1):
            day = (today + timedelta(days=delta)).isoformat()
            for ev in self._get("eventsday.php", d=day, l=self.league_id):
                if str(ev.get("idLeague")) == str(self.league_id):
                    f = normalize_thesportsdb(ev)
                    out[f.fixture_id] = f
        return list(out.values())

    def lookup(self, fixture_ids: list[int]) -> list[Fixture]:
        """Authoritative per-game refresh for markets we already opened."""
        found = []
        for fid in fixture_ids:
            evs = self._get("lookupevent.php", id=fid)
            if evs:
                found.append(normalize_thesportsdb(evs[0]))
        return found
