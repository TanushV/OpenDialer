"""A reusable test list uses real call workflow with simulated providers and isolated contacts."""

from datetime import UTC, datetime

from helpers import finish
from journey import call, get, post, ready, run_jobs
from test_feature_calling import Telnyx, inbound, inbound_task

PHONES = ["+12025550198", "+12025550199"]
ROWS = [
    {"name": "Test Alice", "phone": PHONES[0], "company": "Example Pilot"},
    {"name": "Test Bob", "phone": PHONES[1], "company": "Example Pilot"},
]


def test_settings_test_list_reuses_script_and_queue_without_touching_outreach(workspace, carrier, clock):
    ws = workspace
    campaign = ws["campaign_id"]
    production = post(
        ws, "/v1/leads/import", {"campaign_id": campaign, "list_name": "Outreach", "rows": [ROWS[0]]}
    )["leads"][0]
    uploaded = call(ws, "PUT", "/v1/settings/test-list", json={"rows": ROWS})
    assert [row["phone"] for row in uploaded["contacts"]] == PHONES
    assert production["id"] != uploaded["contacts"][0]["id"]
    assert get(ws, "/v1/settings/test-list") == uploaded
    list_name = uploaded["list_name"]
    queue = {"campaign_id": campaign, "list_names": [list_name]}
    ready(ws)
    post(ws, "/v1/queue/next", queue, status=409)
    post(ws, "/v1/rep/presence", {"status": "paused"})
    assert not carrier.dials

    prep = get(ws, "/v1/preparation")
    assert next(row for row in prep["lists"] if row["list_name"] == list_name)["unrequested"] == 2
    assert post(ws, "/v1/preparation/lists", {"list_name": list_name})["marked"] == 2
    run_jobs(ws, rounds=4)
    clock.now = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
    for iteration in range(2):
        run = post(ws, "/v1/settings/test-list/start")
        assert run["phones"] == PHONES
        ready(ws)
        request = {**queue, "test_only": True, "test_run_id": run["id"]}
        called = []
        for index in range(2):
            reservation = post(ws, "/v1/queue/next", request)
            called.append(reservation["lead"]["phone"])
            placed = post(
                ws,
                "/v1/calls/start",
                {
                    "reservation_id": reservation["reservation_id"],
                    "idempotency_key": f"test-{iteration}-{index}",
                },
            )
            assert placed["is_test"] and placed["script_snapshot"]["fallback"] is False
            assert "time_limit_secs" not in carrier.dials[-1]
            finish(ws, placed, outcome="not_interested")
        assert sorted(called) == PHONES
        assert post(ws, "/v1/queue/next", request, status=409)["detail"]["code"] == "TEST_LIST_COMPLETE"
        assert get(ws, "/v1/workspace")["test_run"] is None
        post(ws, "/v1/queue/next", request, status=409)
    other = get(ws, f"/v1/leads/{production['id']}/packet")["lead"]
    assert other["attempt_count"] == 0 and not other.get("last_outcome")


def test_stopping_or_replacing_a_test_run_invalidates_its_old_reservation(workspace, carrier):
    ws = workspace
    call(ws, "PUT", "/v1/settings/test-list", json={"rows": ROWS})
    run = post(ws, "/v1/settings/test-list/start")
    ready(ws)
    blocked = post(ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"]}, status=409)
    assert blocked["detail"]["code"] == "TEST_SESSION_TEST_LINE_ONLY"
    request = {"campaign_id": ws["campaign_id"], "test_only": True, "test_run_id": run["id"]}
    reservation = post(ws, "/v1/queue/next", request)
    call(ws, "DELETE", "/v1/settings/test-call")
    later = post(ws, "/v1/settings/test-list/start")
    assert later["id"] != run["id"]
    ready(ws)
    post(ws, "/v1/queue/next", request, status=409)
    result = ws["client"].post(
        "/v1/calls/start",
        headers=ws["headers"],
        json={"reservation_id": reservation["reservation_id"], "idempotency_key": "old-test"},
    )
    assert result.status_code in {403, 409}, result.text
    assert not carrier.dials


def test_invalid_test_upload_preserves_the_saved_targets(workspace):
    saved = call(workspace, "PUT", "/v1/settings/test-list", json={"rows": ROWS})
    assert saved["contacts"]
    call(
        workspace,
        "PUT",
        "/v1/settings/test-list",
        status=422,
        json={"rows": [ROWS[0], {"name": "Invalid", "phone": "bad"}]},
    )
    assert get(workspace, "/v1/settings/test-list") == saved
    replaced = call(workspace, "PUT", "/v1/settings/test-list", json={"rows": [ROWS[1]]})
    assert replaced["contacts"] == [saved["contacts"][1]]
    restored = call(workspace, "PUT", "/v1/settings/test-list", json={"rows": ROWS})
    assert restored == saved


def test_callback_matches_the_test_contact_that_placed_the_call(workspace, clock, carrier):
    ws = workspace
    campaign = ws["campaign_id"]
    post(ws, "/v1/leads/import", {"campaign_id": campaign, "list_name": "Outreach", "rows": [ROWS[0]]})
    uploaded = call(ws, "PUT", "/v1/settings/test-list", json={"rows": ROWS[:1]})
    run = post(ws, "/v1/settings/test-list/start")
    ready(ws)
    reservation = post(ws, "/v1/queue/next", {"campaign_id": campaign, "test_run_id": run["id"]})
    placed = post(
        ws,
        "/v1/calls/start",
        {
            "reservation_id": reservation["reservation_id"],
            "idempotency_key": "test-callback",
        },
    )
    finish(ws, placed, outcome="not_interested")
    Telnyx(ws, clock).deliver(
        "call.initiated",
        inbound("test-list-return", PHONES[0], carrier.dials[-1]["from"]),
        "test-list-return",
    )
    run_jobs(ws)
    task = inbound_task(ws, "test-list-return")
    assert task["lead_id"] == uploaded["contacts"][0]["id"]
    assert task["previous_call_id"] == placed["id"]
    assert task["ambiguous"] is False
