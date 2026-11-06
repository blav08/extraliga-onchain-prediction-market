from oracle.planner import ChainMatch, ChainStatus, Kind, plan
from oracle.sources import HOME, normalize_apisports
from tests.helpers import game

NOW = 1_800_000_000
H = 3600


def f(gid, ts, status="NS", h=None, a=None, so=None):
    return normalize_apisports(game(gid, ts, status, h, a, so))


def on_chain(fx, status, start=None):
    return {fx.match_id: ChainMatch(status, start if start is not None else fx.start_ts)}


def kinds(actions):
    return [a.kind for a in actions]


def test_creates_upcoming_match():
    fx = f(1, NOW + 5 * H)
    [a] = plan([fx], {}, NOW)
    assert a.kind is Kind.CREATE and a.start_ts == fx.start_ts


def test_skips_match_too_soon_or_too_far():
    assert plan([f(1, NOW + 60)], {}, NOW) == []             # closes in 1 min
    assert plan([f(2, NOW + 30 * 24 * H)], {}, NOW) == []    # a month away


def test_settles_final_open_match():
    fx = f(1, NOW - 3 * H, "FT", 4, 2)
    [a] = plan([fx], on_chain(fx, ChainStatus.OPEN), NOW)
    assert a.kind is Kind.SETTLE and a.winner == HOME


def test_waits_while_live_or_interrupted():
    for status in ("P2", "BT", "OT", "INTR"):
        fx = f(1, NOW - H, status, 1, 1)
        assert plan([fx], on_chain(fx, ChainStatus.OPEN), NOW) == []


def test_cancels_postponed():
    fx = f(1, NOW + 2 * H, "POST")
    [a] = plan([fx], on_chain(fx, ChainStatus.OPEN), NOW)
    assert a.kind is Kind.CANCEL


def test_cancels_when_kickoff_moved_earlier_than_betting_close():
    # Market says betting closes 20:00, but the game actually starts at 19:00.
    # Between 19:00 and 20:00 people could bet on a running game -> void it.
    fx = f(1, NOW - H, "FT", 3, 0)
    [a] = plan([fx], on_chain(fx, ChainStatus.OPEN, start=NOW), NOW + 3 * H)
    assert a.kind is Kind.CANCEL


def test_cancels_game_live_while_betting_still_open():
    fx = f(1, NOW + 10 * 60, "P1", 0, 0)      # provider says live, timestamp in tolerance
    [a] = plan([fx], on_chain(fx, ChainStatus.OPEN), NOW)
    assert a.kind is Kind.CANCEL


def test_later_reschedule_is_fine():
    # Start moved LATER: betting simply closes early, no integrity issue
    fx = f(1, NOW + 5 * H, "NS")
    assert plan([fx], on_chain(fx, ChainStatus.OPEN, start=NOW + 2 * H), NOW) == []


def test_ambiguous_final_needs_human():
    fx = f(1, NOW - 3 * H, "AP", 2, 2, None)
    [a] = plan([fx], on_chain(fx, ChainStatus.OPEN), NOW)
    assert a.kind is Kind.WARN


def test_terminal_states_are_left_alone():
    fx = f(1, NOW - 3 * H, "FT", 1, 0)
    assert plan([fx], on_chain(fx, ChainStatus.SETTLED), NOW) == []
    assert plan([fx], on_chain(fx, ChainStatus.CANCELLED), NOW) == []


def test_idempotent_after_execution():
    # Run 1 plans a create; after it lands, run 2 must plan nothing
    fx = f(1, NOW + 5 * H)
    assert kinds(plan([fx], {}, NOW)) == [Kind.CREATE]
    assert plan([fx], on_chain(fx, ChainStatus.OPEN), NOW) == []


def test_money_releasing_actions_go_first():
    new = f(1, NOW + 5 * H)
    done = f(2, NOW - 3 * H, "FT", 1, 0)
    chain = on_chain(done, ChainStatus.OPEN)
    assert kinds(plan([new, done], chain, NOW)) == [Kind.SETTLE, Kind.CREATE]
