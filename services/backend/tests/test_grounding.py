"""R9.7: research review judges facts strictly and a labeled bridge only for contradiction."""


def test_a_labeled_inference_is_rejected_only_when_contradicted():
    from app.grounding import accept_review, review_request

    request = review_request(
        "script",
        [
            {"field": "a:bridge", "value": "Sounds like a busy quarter.", "evidence": [], "inference": True},
            {"field": "b:bridge", "value": "You must be shrinking.", "evidence": [], "inference": True},
        ],
        [{"id": "f1", "statement": "They added two buildings."}],
    )
    data = {
        "fingerprint": request["fingerprint"],
        "verdicts": [
            {"field": "a:bridge", "verdict": "UNSUPPORTED", "evidence": []},
            {"field": "b:bridge", "verdict": "CONTRADICTED", "evidence": ["f1"]},
        ],
    }
    assert accept_review(request, data, "r1", partial=True).rejected == ("b:bridge",)
