from app.scheduling.models import pooled_predictions


def test_partial_pooling_separates_human_right_party_and_next_step():
    sparse = pooled_predictions([], segment="roofing", hour=11)
    history = [
        {"segment": "roofing", "local_hour": 11, "outcome": "gatekeeper", "duration_seconds": 60}
        for _ in range(30)
    ]
    learned = pooled_predictions(history, segment="roofing", hour=11)
    assert 0 < sparse["p_human"] < 1
    assert learned["p_human"] > sparse["p_human"]
    assert learned["p_right_party_given_human"] < learned["p_human"]
    assert learned["expected_rep_seconds"] > 0
