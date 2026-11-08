"""R6.4: voicemail detection is compared with what the rep found when they stayed."""

from app.calling.detection import ENOUGH, report


def test_only_stayed_calls_can_show_a_wrong_detection():
    calls = [
        {"outcome": "voicemail", "machine_detected": True},
        {"outcome": "not_interested", "machine_detected": True, "stay_on_voicemail": True},
        {"outcome": "voicemail", "machine_detected": True, "stay_on_voicemail": True},
        {"outcome": "voicemail"},
        {"outcome": "right_party"},
        {"outcome": "voicemail", "machine_detected": True, "is_test": True},
        {"outcome": None, "machine_detected": True},
    ]
    assert report(calls) == {
        "calls": 5,
        "ready": False,
        "detected_as_machine": 3,
        "skipped": 1,
        "stayed": 2,
        "stayed_and_reached_a_person": 1,
        "voicemail_not_detected": 1,
    }
    assert report([{"outcome": "busy"}] * ENOUGH)["ready"] is True


def test_the_report_is_served(workspace):
    client, headers = workspace["client"], workspace["headers"]
    body = client.get("/v1/calling/voicemail-report", headers=headers).json()
    assert body["ready"] is False and "stayed_and_reached_a_person" in body
