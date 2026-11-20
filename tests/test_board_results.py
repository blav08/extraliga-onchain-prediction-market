"""Settled / cancelled games must stay on the board so winners can still collect."""
import json

from oracle.runner import RESULTS_KEEP_S, closed_markets_from_log
from oracle.sources import Phase, match_id_for

NOW = 1_795_000_000
DAY = 24 * 3600


def entry(action, fixture_id, start_ts, ok=True, dry_run=False, score=(3, 4), match="Litvinov vs Motor"):
    """One audit-log line in the same shape ledger.append_log writes."""
    return {
        "at": start_ts + 3 * 3600, "action": action, "dry_run": dry_run,
        "match_id": "0x" + match_id_for("thesportsdb", fixture_id).hex().removeprefix("0x"),
        "match": match, "reason": "test", "winner": "AWAY",
        "evidence": {"provider": "thesportsdb", "fixture_id": fixture_id, "status": "FT",
                     "score": list(score), "shootout": None, "start_ts": start_ts},
        "ok": ok, "tx_hash": "0xabc", "error": None,
    }


def write_log(tmp_path, entries):
    path = tmp_path / "oracle-log.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def test_settled_game_comes_back_with_teams_and_score(tmp_path):
    log = write_log(tmp_path, [entry("create", 1, NOW - 7 * DAY), entry("settle", 1, NOW - 7 * DAY)])
    [f] = closed_markets_from_log(log, NOW)
    assert (f.home, f.away) == ("Litvinov", "Motor")
    assert (f.score_home, f.score_away) == (3, 4)
    assert f.phase is Phase.FINAL
    # same deterministic id as the market on chain, so the web app finds the stakes
    assert f.match_id == match_id_for("thesportsdb", 1)


def test_cancelled_game_is_void(tmp_path):
    log = write_log(tmp_path, [entry("cancel", 2, NOW - DAY)])
    [f] = closed_markets_from_log(log, NOW)
    assert f.phase is Phase.VOID


def test_old_failed_and_dry_run_entries_are_ignored(tmp_path):
    log = write_log(tmp_path, [
        entry("settle", 3, NOW - RESULTS_KEEP_S - DAY),  # older than the keep window
        entry("settle", 4, NOW - DAY, ok=False),         # failed transaction
        entry("settle", 5, NOW - DAY, dry_run=True),     # dry run, nothing on chain
        entry("create", 6, NOW + DAY),                   # still open, not a result
    ])
    assert closed_markets_from_log(log, NOW) == []


def test_logos_are_carried_over_from_previous_board(tmp_path):
    log = write_log(tmp_path, [entry("settle", 7, NOW - DAY)])
    mid = "0x" + match_id_for("thesportsdb", 7).hex().removeprefix("0x")
    board = tmp_path / "board.json"
    board.write_text(json.dumps({"matches": [{"match_id": mid, "home_logo": "h.png", "away_logo": "a.png"}]}))
    [f] = closed_markets_from_log(log, NOW, previous_board=board)
    assert (f.home_logo, f.away_logo) == ("h.png", "a.png")


def test_missing_log_gives_nothing(tmp_path):
    assert closed_markets_from_log(tmp_path / "nope.jsonl", NOW) == []
