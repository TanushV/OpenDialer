from datetime import timedelta

from conftest import disposition_body, start
from journey import call, get
from test_telnyx_contracts import event


def test_connect_rate_requires_confirmed_audio_and_keeps_carrier_blocks(workspace, clock):
    first = start(workspace)
    assert event(workspace, first, "call.answered", {"timing_source": "provider"}).status_code == 200
    clock.now += timedelta(milliseconds=200)
    assert event(workspace, first, "call.bridged", {"timing_source": "provider"}).status_code == 200
    clock.now += timedelta(seconds=3)
    assert event(workspace, first, "call.hangup").status_code == 200
    client, headers = workspace["client"], workspace["headers"]
    call(
        workspace,
        "POST",
        f"/v1/calls/{first['id']}/disposition",
        json=disposition_body(
            client,
            headers,
            first["id"],
            outcome="not_interested",
            contact_role="right_party",
            audio_result="two_way",
            screened=True,
        ),
    )
    second = start(workspace, key="blocked-attempt")
    assert event(workspace, second, "call.hangup", {"sip_hangup_cause": "603"}).status_code == 200
    report = get(workspace, "/v1/admin/connect-report")
    total = report["rolling_28_days"]
    assert total["attempts"] == 2 and total["connects"] == 1 and total["connect_rate"] == 0.5
    assert total["intended_prospect"] == 1 and total["screened_to_human"] == 1
    assert total["carrier_blocks"] == 1 and total["short_answered"] == 1
    assert total["answer_to_bridge_p95_seconds"] == 0.2
    assert len(report["daily"]) == len(report["weekly"]) == 1
    assert report["by_carrier"][0]["key"] == "Unknown"


def test_registry_export_contains_the_whole_workspace_inventory(workspace):
    exported = get(workspace, "/v1/admin/numbers/registry-export")["csv"].splitlines()
    assert exported[0] == "phone_number"
    assert "+14155550100" in exported and "+12125550100" in exported


def test_an_explicit_carrier_lookup_is_cached_for_the_lead_and_endpoint(workspace, carrier, monkeypatch):
    lead = get(workspace, "/v1/workspace/leads")["items"][0]
    lookups = []

    async def lookup(phone):
        lookups.append(phone)
        return {"carrier": "Example Wireless", "line_type": "mobile"}

    monkeypatch.setattr(carrier, "lookup_number", lookup, raising=False)
    path = f"/v1/admin/leads/{lead['id']}/carrier-lookup"
    call(workspace, "POST", path, json={"approve_charge": False}, status=422)
    for _ in range(2):
        result = call(workspace, "POST", path, json={"approve_charge": True})
        assert result["carrier"] == "Example Wireless" and result["line_type"] == "mobile"
    assert lookups == [lead["phone"]]


def test_signed_mailbox_beep_dispatches_without_waiting_for_recovery(workspace, carrier, clock):
    from journey import run_jobs
    from test_feature_calling import Telnyx

    attempt = start(workspace)
    telnyx = Telnyx(workspace, clock)
    telnyx.deliver(
        "call.machine.premium.greeting.ended",
        {"call_control_id": attempt["provider_id"], "result": "beep_detected"},
    )
    run_jobs(workspace)
    assert [row["provider_id"] for row in carrier.hangups] == [attempt["provider_id"]]
