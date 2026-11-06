"""Builders for API-Sports-shaped game dicts, so tests read like scenarios."""


def game(gid, ts, status="NS", home=None, away=None, penalties=None,
         home_team="HC Sparta Praha", away_team="HC Dynamo Pardubice"):
    return {
        "id": gid,
        "timestamp": ts,
        "status": {"short": status, "long": status},
        "teams": {
            "home": {"id": 1, "name": home_team, "logo": None},
            "away": {"id": 2, "name": away_team, "logo": None},
        },
        "scores": {"home": home, "away": away},
        "periods": {"first": None, "second": None, "third": None,
                    "overtime": None, "penalties": penalties},
    }
