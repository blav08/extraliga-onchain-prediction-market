import pytest

from oracle.sources import AWAY, HOME, Phase, match_id_for, normalize_apisports
from tests.helpers import game


@pytest.mark.parametrize("status,h,a,so,expected", [
    ("FT", 3, 1, None, HOME),       # regulation win
    ("AOT", 2, 3, None, AWAY),      # overtime win, OT goal included in score
    ("AP", 2, 2, "2-3", AWAY),      # shootout, score level, decided by SO line
    ("AP", 3, 2, "3-2", HOME),      # shootout where provider already counts SO goal
    ("AP", 2, 2, None, None),       # shootout but no SO data: refuse to guess
    ("P3", 2, 1, None, None),       # still playing: no winner yet
    ("POST", None, None, None, None),
])
def test_winner(status, h, a, so, expected):
    f = normalize_apisports(game(1, 0, status, h, a, so))
    assert f.winner() == expected


def test_unknown_status_is_uncertain():
    # A new status code we have never seen must never trigger an action
    assert normalize_apisports(game(1, 0, "XYZ")).phase is Phase.UNCERTAIN


def test_match_id_is_deterministic_and_provider_scoped():
    assert match_id_for("apisports-hockey", 42) == match_id_for("apisports-hockey", "42")
    assert match_id_for("apisports-hockey", 42) != match_id_for("other", 42)
