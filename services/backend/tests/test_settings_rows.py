"""R7.1 to R7.3: a Settings row is green only after a real check, and a failed check says why."""

from app.calling.setup import BY_ID, FAILED, MISSING, OK, OPTIONAL, UNCHECKED, check_record, rows

LIVE_OK = {
    key: (True, "fine") for key in ("connection", "numbers", "chatgpt", "script", "hours", "test_phone")
}


def state(config, checks=None, live=None):
    return {r["id"]: r["state"] for r in rows(config, checks or {}, live or LIVE_OK)}


def test_a_saved_key_is_not_green_until_it_is_checked():
    assert state({})["telnyx"] == MISSING
    assert state({"telnyx_api_key": "KEY1"})["telnyx"] == UNCHECKED
    passed = {"telnyx": check_record(BY_ID["telnyx"], {"telnyx_api_key": "KEY1"}, True, "accepted", "t")}
    assert state({"telnyx_api_key": "KEY1"}, passed)["telnyx"] == OK


def test_a_changed_key_needs_a_new_check_and_a_failed_check_says_why():
    passed = {"telnyx": check_record(BY_ID["telnyx"], {"telnyx_api_key": "KEY1"}, True, "accepted", "t")}
    assert state({"telnyx_api_key": "KEY2"}, passed)["telnyx"] == UNCHECKED
    failed = {
        "openrouter": check_record(BY_ID["openrouter"], {"openrouter_api_key": "K"}, False, "refused", "t")
    }
    row = next(r for r in rows({"openrouter_api_key": "K"}, failed, LIVE_OK) if r["id"] == "openrouter")
    assert (row["state"], row["detail"]) == (FAILED, "refused")


def test_live_rows_follow_the_app_and_optional_rows_never_block():
    live = {**LIVE_OK, "numbers": (False, "Add a caller number")}
    assert state({}, live=live)["numbers"] == FAILED
    assert {state({})[key] for key in ("realvalidito", "hubspot", "treg")} == {OPTIONAL}
    every = rows({}, {}, LIVE_OK)
    assert all(r["where"] and r["powers"] for r in every)


def test_the_settings_route_lists_rows_and_checks_on_request(workspace):
    client, headers = workspace["client"], workspace["headers"]
    body = client.get("/v1/settings/rows", headers=headers).json()
    assert [r["id"] for r in body["rows"]][:3] == ["telnyx", "connection", "numbers"]
    assert body["status"] is None or body["status"]["code"]
    assert client.post("/v1/settings/rows/openrouter/check", headers=headers).status_code == 409
    client.put("/v1/admin/settings", headers=headers, json={"openrouter_api_key": "sk-or-test"})
    checked = client.post("/v1/settings/rows/openrouter/check", headers=headers).json()
    row = next(r for r in checked["rows"] if r["id"] == "openrouter")
    assert row["state"] == OK and "checked_at" in row
    settings = client.get("/v1/admin/settings", headers=headers).json()
    assert "_setup_checks" not in settings and "setup_checks" not in settings


async def test_key_checks_report_accepted_refused_and_unreachable():
    import httpx
    from app.providers import openrouter, telnyx

    def answer(status):
        return httpx.MockTransport(lambda request: httpx.Response(status, json={}))

    async with httpx.AsyncClient(transport=answer(200)) as http:
        assert await telnyx.check_key("k", http) == (True, "Telnyx accepted this key")
    async with httpx.AsyncClient(transport=answer(401)) as http:
        ok, detail = await openrouter.check_key("k", http)
        assert not ok and "refused" in detail

    def down(request):
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        ok, detail = await telnyx.check_key("k", http)
        assert not ok and "could not be reached" in detail


def test_a_row_never_set_reads_not_set_and_a_broken_one_reads_failed():
    """A fresh install shows what to add, not a page of failures (R7.8)."""
    from app.calling.setup import SPECS, row

    connection = next(spec for spec in SPECS if spec.id == "connection")
    fresh = row(connection, {}, {}, {"connection": (False, "Import your calling setup file", False)})
    broken = row(connection, {}, {}, {"connection": (False, "Call events are reconnecting", True)})
    assert (fresh["state"], broken["state"]) == ("missing", "failed")
    assert fresh["detail"] == "Import your calling setup file"
