"""TheSportsDB normalization, using the exact field shapes the API returned on 2026-11-06."""
import json

from oracle.planner import Kind
from oracle.runner import open_markets_from_log
from oracle.sources import AWAY, HOME, Phase, normalize_thesportsdb


def ev(**kw):
    base = {"idEvent": "2480614", "idLeague": "4923", "strTimestamp": "2026-09-16T15:30:00",
            "strHomeTeam": "Plzen", "strAwayTeam": "Bílí Tygři Liberec",
            "intHomeScore": "2", "intAwayScore": "1", "strStatus": "AP", "strPostponed": "no"}
    base.update(kw)
    return base


def test_shootout_win_counted_in_score():
    f = normalize_thesportsdb(ev())
    assert f.phase is Phase.FINAL and f.winner() == HOME
    assert f.start_ts == 1789572600  # 2026-09-16 15:30 UTC: naive timestamps are UTC


def test_overtime_away_win():
    f = normalize_thesportsdb(ev(strStatus="AOT", intHomeScore="2", intAwayScore="3"))
    assert f.winner() == AWAY


def test_not_started_has_no_score():
    f = normalize_thesportsdb(ev(strStatus="NS", intHomeScore=None, intAwayScore=None))
    assert f.phase is Phase.SCHEDULED and f.score_home is None and f.winner() is None


def test_postponed_flag_wins_over_status():
    assert normalize_thesportsdb(ev(strStatus="NS", strPostponed="yes")).phase is Phase.VOID


def test_level_final_without_shootout_data_is_not_guessed():
    assert normalize_thesportsdb(ev(intHomeScore="2", intAwayScore="2")).winner() is None


def test_unknown_status_is_uncertain():
    assert normalize_thesportsdb(ev(strStatus="Weird")).phase is Phase.UNCERTAIN


def test_open_markets_from_audit_log(tmp_path):
    log = tmp_path / "oracle-log.jsonl"
    rows = [
        ("create", 1, True, False), ("create", 2, True, False), ("settle", 1, True, False),
        ("create", 3, False, False),   # failed create: no market exists
        ("create", 4, True, True),     # dry run: no market exists
    ]
    log.write_text("\n".join(json.dumps({
        "action": a, "ok": ok, "dry_run": dry,
        "evidence": {"provider": "thesportsdb", "fixture_id": fid}}) for a, fid, ok, dry in rows))
    assert open_markets_from_log(log, "thesportsdb") == {2}
    assert open_markets_from_log(log, "apisports-hockey") == set()
