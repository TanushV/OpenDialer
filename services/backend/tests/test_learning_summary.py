"""R8.8: the learning card says what has been learned, from real calls only."""

from app.learning.summary import summary


def row(hour, human, *, outcome="not_interested", source="leadocean", trigger="hiring", conversion=None):
    return {
        "human": human,
        "outcome": outcome,
        "conversion": conversion,
        "features": {"local_hour": hour, "number_source": source, "trigger_type": trigger},
    }


def test_the_card_ranks_hours_and_sources_and_names_the_live_model():
    rows = (
        [row(10, 1)] * 6
        + [row(15, 0)] * 6
        + [row(11, 1, outcome="wrong_party", source="tomba")] * 5
        + [row(11, 0, source="tomba")] * 5
        + [{"human": None}] * 3
    )
    models = [
        {"active": True, "promoted_at": "2026-11-08T10:00:00+00:00"},
        {"rolled_back_at": "2026-11-07T09:00:00"},
    ]
    card = summary(rows, models, required_rows=20)
    assert card["calls_used"] == 22 and card["learning"] is True
    assert card["best_hours"][0] == {"value": 10, "calls": 6, "rate": 1.0}
    sources = {r["value"]: r["rate"] for r in card["wrong_party_by_number_source"]}
    assert sources == {"leadocean": 0.0, "tomba": 1.0}
    assert card["model"] == {"live": True, "promoted_at": "2026-11-08T10:00:00+00:00"}
    assert card["last_change"] == {"at": "2026-11-08T10:00:00+00:00", "kind": "promoted"}


def test_small_groups_are_not_ranked_and_an_empty_workspace_says_so():
    card = summary([row(9, 1)] * 2, [])
    assert card["best_hours"] == [] and card["learning"] is False and card["last_change"] is None


def test_the_summary_route_answers_for_a_new_workspace(workspace):
    body = workspace["client"].get("/v1/learning/summary", headers=workspace["headers"]).json()
    assert (
        body["calls_used"] == 0
        and body["learning"] is False
        and body["model"] == {"live": False, "promoted_at": None}
    )
