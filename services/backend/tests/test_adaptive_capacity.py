from app.optimization.numbering import adaptive_daily_limit


def test_capacity_adapts_to_delivery_within_reviewed_maximum():
    number = {"daily_limit": 100, "outbound_ready": True}
    assert adaptive_daily_limit({"daily_limit": 40}, {"recommended_action": "retain_reviewed_workload"}) == 40
    assert adaptive_daily_limit(number, {"recommended_action": "retain_reviewed_workload"}) == 90
    assert (
        adaptive_daily_limit(
            number,
            {"recommended_action": "review_workload", "delivery_samples": 100, "delivery_success_rate": 0.8},
        )
        == 72
    )
    assert adaptive_daily_limit(number, {"recommended_action": "hold_outbound"}) == 0
    assert adaptive_daily_limit({**number, "outbound_ready": False}, {}) == 0
