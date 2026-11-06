"""Owner's list controls: one contact/history, explicit scope and reversible removal."""

from datetime import timedelta

from journey import call, get, hang_up, import_leads, post, ready, wrap_up


def imported(ws, name, phone, timezone="America/Los_Angeles"):
    return import_leads(ws, [{"name": name, "phone": phone, "timezone": timezone}], list_name=name)["leads"][
        0
    ]


def manage(ws, list_name, action, **extra):
    return call(
        ws,
        "PATCH",
        "/v1/lists",
        json={"campaign_id": ws["campaign_id"], "list_name": list_name, "action": action, **extra},
    )


def bulk(ws, ids, action, **extra):
    return post(ws, "/v1/leads/bulk", {"lead_ids": ids, "action": action, **extra})


def choose(ws, names=None, **extra):
    ready(ws)
    body = {"campaign_id": ws["campaign_id"], **extra}
    if names is not None:
        body["list_names"] = names
    return post(ws, "/v1/queue/next", body)


def begin(ws, reservation, key):
    return post(
        ws, "/v1/calls/start", {"reservation_id": reservation["reservation_id"], "idempotency_key": key}
    )


def test_duplicate_import_links_both_lists_without_a_second_contact(workspace):
    first = imported(workspace, "West", "+14155550881")
    again = import_leads(workspace, [{"phone": first["phone"]}], list_name="Partners")
    assert again["duplicates"] == 1
    assert again["leads"][0]["id"] == first["id"]
    assert set(again["leads"][0]["list_names"]) == {"West", "Partners"}
    lists = get(workspace, "/v1/lists")
    assert next(x for x in lists["lists"] if x["name"] == "Partners")["total"] == 1
    manage(workspace, "West", "archive")
    chosen = choose(workspace, ["Partners"])
    assert chosen["lead"]["id"] == first["id"]


def test_list_scope_and_archive_are_rechecked_before_dial(workspace):
    west = imported(workspace, "West", "+14155550882")
    imported(workspace, "East", "+12125550883", "America/New_York")
    reservation = choose(workspace, ["West"])
    assert reservation["lead"]["id"] == west["id"]
    manage(workspace, "West", "archive")
    denied = post(
        workspace,
        "/v1/calls/start",
        {"reservation_id": reservation["reservation_id"], "idempotency_key": "archived-reservation"},
        status=403,
    )
    assert "OUTREACH_HOLD" in denied["detail"]["reasons"]
    manage(workspace, "West", "restore")
    assert begin(workspace, reservation, "restored-reservation")["lead_id"] == west["id"]


def test_bulk_lifecycle_keeps_history_and_can_move_and_remove_membership(workspace):
    lead = imported(workspace, "Original", "+14155550884")
    for action, field, value in [
        ("pause", "dial_paused", True),
        ("resume", "dial_paused", False),
        ("archive", "archived", True),
        ("restore", "archived", False),
    ]:
        result = bulk(workspace, [lead["id"]], action)
        assert result["leads"][0][field] is value
    bulk(workspace, [lead["id"]], "move", list_name="Moved")
    manage(workspace, "Moved", "rename", name="Renamed")
    packet = get(workspace, f"/v1/leads/{lead['id']}/packet")
    assert packet["lead"]["list_names"] == ["Renamed"]
    bulk(workspace, [lead["id"]], "location", timezone="America/New_York", state="NY")
    packet = get(workspace, f"/v1/leads/{lead['id']}/packet")
    assert packet["lead"]["timezone"] == "America/New_York"
    assert packet["lead"]["state"] == "NY"
    bulk(workspace, [lead["id"]], "remove", list_name="Renamed")
    assert get(workspace, f"/v1/leads/{lead['id']}/packet")["lead"]["list_names"] == []
    assert get(workspace, f"/v1/leads/{lead['id']}/packet")["lead"]["id"] == lead["id"]


def test_remove_rejects_an_unrelated_list_without_claiming_success(workspace):
    lead = imported(workspace, "Named source", "+14155550651")
    before = get(workspace, f"/v1/leads/{lead['id']}/packet")["lead"]
    post(
        workspace,
        "/v1/leads/bulk",
        {"lead_ids": [lead["id"]], "action": "remove", "list_name": ""},
        status=422,
    )
    after = get(workspace, f"/v1/leads/{lead['id']}/packet")["lead"]
    assert after["version"] == before["version"]
    assert after["list_names"] == ["Named source"]


def test_remove_counts_only_members_and_keeps_other_lists(workspace):
    member = imported(workspace, "Remove source", "+14155550652")
    other = imported(workspace, "Keep source", "+14155550653")
    import_leads(workspace, [{"phone": member["phone"]}], list_name="Keep source")
    result = bulk(workspace, [member["id"], other["id"]], "remove", list_name="Remove source")
    assert result["updated"] == 1 and result["unchanged"] == 1
    assert result["leads"][0]["list_names"] == ["Keep source"]


def test_removed_list_visibility_keeps_shared_contacts_and_restore_history(workspace):
    baseline = get(workspace, "/v1/workspace/leads")["total"]
    sole = imported(workspace, "Remove entire list", "+14155550654")
    shared = import_leads(
        workspace,
        [{"name": "Shared", "phone": "+14155550655", "timezone": "America/Los_Angeles"}],
        list_name="Remove entire list",
    )["leads"][0]
    import_leads(workspace, [{"phone": shared["phone"]}], list_name="Other active list")
    manage(workspace, "Remove entire list", "archive")
    choices = get(workspace, "/v1/admin/numbers/summary")["lists"]
    assert "Remove entire list" not in choices
    assert "Other active list" in choices
    active = get(workspace, "/v1/workspace/leads", params={"filter": "active"})
    removed = get(workspace, "/v1/workspace/leads", params={"filter": "removed"})
    assert sole["id"] not in {row["id"] for row in active["items"]}
    assert shared["id"] in {row["id"] for row in active["items"]}
    assert {row["id"] for row in removed["items"]} == {sole["id"]}
    assert removed["total"] == 1
    assert get(workspace, "/v1/workspace/leads")["total"] == baseline + 2
    manage(workspace, "Remove entire list", "restore")
    assert get(workspace, "/v1/workspace/leads", params={"filter": "active"})["total"] == baseline + 2
    assert get(workspace, f"/v1/leads/{sole['id']}/packet")["lead"]["id"] == sole["id"]


def test_removed_lead_and_last_membership_have_recoverable_views(workspace):
    baseline = get(workspace, "/v1/workspace/leads")["total"]
    lead = imported(workspace, "Recoverable", "+14155550656")
    bulk(workspace, [lead["id"]], "archive")
    assert get(workspace, "/v1/workspace/leads", params={"filter": "active"})["total"] == baseline
    assert get(workspace, "/v1/workspace/leads", params={"filter": "removed"})["total"] == 1
    bulk(workspace, [lead["id"]], "restore")
    bulk(workspace, [lead["id"]], "remove", list_name="Recoverable")
    assert get(workspace, "/v1/workspace/leads", params={"filter": "active"})["total"] == baseline
    assert get(workspace, "/v1/workspace/leads", params={"filter": "removed"})["total"] == 1
    bulk(workspace, [lead["id"]], "add", list_name="Recovered")
    assert get(workspace, "/v1/workspace/leads", params={"filter": "active"})["total"] == baseline + 1


def test_removed_and_active_views_filter_before_pagination(workspace):
    baseline = get(workspace, "/v1/workspace/leads")["total"]
    rows = [
        {"name": f"Removed {i}", "phone": f"+14155556{i:03}", "timezone": "America/Los_Angeles"}
        for i in range(60)
    ]
    leads = import_leads(workspace, rows, list_name="Paged removal")["leads"]
    for row in rows[-5:]:
        import_leads(workspace, [{"phone": row["phone"]}], list_name="Keep tail")
    manage(workspace, "Paged removal", "archive")
    active = get(workspace, "/v1/workspace/leads", params={"filter": "active"})
    assert active["total"] == baseline + 5 and len(active["items"]) == baseline + 5
    first = get(workspace, "/v1/workspace/leads", params={"filter": "removed"})
    assert first["total"] == 55 and len(first["items"]) == 50
    second = get(
        workspace,
        "/v1/workspace/leads",
        params={"filter": "removed", "cursor": first["next_cursor"]},
    )
    assert len(second["items"]) == 5 and second["next_cursor"] is None
    assert len({r["id"] for r in first["items"] + second["items"]}) == 55
    assert get(workspace, "/v1/workspace/leads")["total"] == baseline + len(leads)


def test_outside_callback_requires_an_explicit_choice(workspace, clock):
    east = imported(workspace, "East callback", "+12125550885", "America/New_York")
    west = imported(workspace, "West", "+14155550886")
    attempt = begin(workspace, choose(workspace, ["East callback"]), "callback-first")
    hang_up(workspace, attempt["id"])
    due = clock() + timedelta(minutes=1)
    wrap_up(
        workspace,
        attempt["id"],
        outcome="callback_requested",
        callback_at=due.isoformat(),
        callback_note="Requested a callback",
    )
    clock.now += timedelta(minutes=2)
    data = get(workspace, "/v1/lists", params={"list_names": "West"})
    assert east["id"] in [x["id"] for x in data["outside_callbacks"]]
    chosen = choose(workspace, ["West"], callback_lead_id=east["id"])
    assert chosen["lead"]["id"] == east["id"]
    assert west["id"] != chosen["lead"]["id"]
    callback_call = begin(workspace, chosen, "explicit-outside-callback")
    assert callback_call["state"] != "FINALIZED"
    hang_up(workspace, callback_call["id"])
    wrap_up(workspace, callback_call["id"], outcome="no_answer")


def test_invalid_bulk_edit_is_atomic_and_empty_selection_never_means_all(workspace):
    lead = imported(workspace, "West", "+14155550887")
    post(
        workspace,
        "/v1/leads/bulk",
        {"lead_ids": [lead["id"]], "action": "location", "timezone": "made/up"},
        status=422,
    )
    assert get(workspace, f"/v1/leads/{lead['id']}/packet")["lead"]["timezone"] == "America/Los_Angeles"
    ready(workspace)
    post(workspace, "/v1/queue/next", {"campaign_id": workspace["campaign_id"], "list_names": []}, status=409)


def test_paused_selected_list_does_not_silently_fall_back_to_another_list(workspace):
    imported(workspace, "Selected", "+14155550893")
    imported(workspace, "Other", "+14155550894")
    manage(workspace, "Selected", "pause")
    ready(workspace)
    post(
        workspace,
        "/v1/queue/next",
        {"campaign_id": workspace["campaign_id"], "list_names": ["Selected"]},
        status=409,
    )
    manage(workspace, "Selected", "resume")
    assert choose(workspace, ["Selected"])["lead"]["list_name"] == "Selected"


def test_membership_move_after_reservation_is_rechecked_and_bulk_missing_ids_are_atomic(workspace):
    lead = imported(workspace, "Selected", "+14155550895")
    reservation = choose(workspace, ["Selected"])
    bulk(workspace, [lead["id"]], "move", list_name="Other")
    denied = post(
        workspace,
        "/v1/calls/start",
        {"reservation_id": reservation["reservation_id"], "idempotency_key": "moved-reservation"},
        status=403,
    )
    assert "OUTREACH_HOLD" in denied["detail"]["reasons"]
    post(
        workspace, "/v1/leads/bulk", {"lead_ids": [lead["id"], "missing-id"], "action": "archive"}, status=404
    )
    assert not get(workspace, f"/v1/leads/{lead['id']}/packet")["lead"].get("archived")


def test_secondary_membership_is_searchable_and_planned_without_duplicate_contacts(workspace):
    lead = imported(workspace, "Primary", "+14155550896")
    import_leads(workspace, [{"phone": lead["phone"]}], list_name="Secondary")
    page = get(workspace, "/v1/workspace/leads", params={"list_name": "Secondary"})
    assert [item["id"] for item in page["items"]] == [lead["id"]]
    manage(workspace, "Secondary", "rename", name="Renamed secondary")
    page = get(workspace, "/v1/workspace/leads", params={"list_name": "Renamed secondary"})
    assert page["total"] == 1
    assert "Renamed secondary" in get(workspace, "/v1/admin/numbers/summary")["lists"]


def test_callable_count_respects_company_spacing_as_well_as_the_clock(workspace):
    import_leads(
        workspace,
        [
            {
                "name": "One",
                "phone": "+14155550897",
                "company": "Same company",
                "timezone": "America/Los_Angeles",
            },
            {
                "name": "Two",
                "phone": "+14155550898",
                "company": "Same company",
                "timezone": "America/Los_Angeles",
            },
        ],
        list_name="Company group",
    )
    attempt = begin(workspace, choose(workspace, ["Company group"]), "same-company")
    hang_up(workspace, attempt["id"])
    wrap_up(workspace, attempt["id"], outcome="no_answer")
    summary = get(workspace, "/v1/lists")
    row = next(row for row in summary["lists"] if row["name"] == "Company group")
    assert row["total"] == 2
    assert row["callable_now"] == 0


def test_shared_preparation_progress_follows_secondary_list_membership(workspace):
    from journey import run_jobs

    lead = import_leads(
        workspace,
        [{"name": "Prepared", "phone": "+14155550899", "timezone": "America/Los_Angeles"}],
        list_name="Prepared primary",
        prepare=True,
    )["leads"][0]
    run_jobs(workspace, 3)
    import_leads(workspace, [{"phone": lead["phone"]}], list_name="Prepared secondary")
    lists = get(workspace, "/v1/preparation")["lists"]
    assert next(row for row in lists if row["list_name"] == "Prepared secondary")["total"] == 1


def test_pausing_a_list_marks_its_saved_capacity_plan_stale(workspace):
    from journey import run_jobs

    imported(workspace, "Capacity list", "+14155550900")
    post(
        workspace,
        "/v1/admin/numbers/proposal",
        {"campaign_id": workspace["campaign_id"], "list_name": "Capacity list"},
    )
    run_jobs(workspace, 4)
    before = get(workspace, "/v1/admin/numbers/summary", params={"list_name": "Capacity list"})["forecast"]
    assert before["projection"]["status"] == "ready"
    manage(workspace, "Capacity list", "pause")
    after = get(workspace, "/v1/admin/numbers/summary", params={"list_name": "Capacity list"})["forecast"]
    assert after["projection"]["status"] == "stale"
