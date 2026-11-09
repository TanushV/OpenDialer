"""R9.2 and R9.3: one intake path and column map for CSV and HubSpot, read from the shape of the owner's rows."""

from app.leads.intake import check_map, guess_map, read, signal_urls

# The owner's call sheet columns (section 3), with made-up values.
CSV = {
    "name": "Liam Example",
    "first name": "Liam",
    "company": "Brighterway",
    "phone": "+16175550100",
    "title": "VP Sales",
    "contact headline": "Building revenue teams",
    "industry": "Logistics",
    "employees (estimate)": "120",
    "finder": "blitzapi",
    "network status": "LIVE",
    "signal summaries": "Hiring 3 SDRs; outbound push | Opened Austin office",
    "signal dates": "2026-10-20; 2026-09-02",
    "signal urls": "https://b.example/jobs; https://b.example/news",
    "email": "liam@b.example",
    "apollo person id": "abc123",
    "salesforge contact ids": "x9",
    "record id": "r-1",
    "fit evidence": "Same sentence on every row",
}
HUBSPOT = {
    "firstname": "Liam",
    "name": "Liam Example",
    "company": "Brighterway",
    "phone": "+16175550100",
    "jobtitle": "VP Sales",
    "industry": "Logistics",
    "numberofemployees": "120",
    "signal summaries": "Hiring 3 SDRs; outbound push | Opened Austin office",
    "signal dates": "2026-10-20; 2026-09-02",
    "signal urls": "https://b.example/jobs; https://b.example/news",
    "hs_object_id": "901",
    "email": "liam@b.example",
}


def test_the_owner_columns_map_to_their_roles_with_a_delimiter_per_column():
    found = guess_map(CSV)
    assert found["signal_summaries"] == {"column": "signal summaries", "delimiter": "|"}
    assert found["signal_dates"]["delimiter"] == found["signal_urls"]["delimiter"] == ";"
    assert found["size"]["column"] == "employees (estimate)" and found["number_source"]["column"] == "finder"
    lead = read(CSV, found)
    # A semicolon inside a summary does not split it: summaries split on the pipe.
    assert lead["signals"] == {
        "aligned": True,
        "items": [
            {
                "summary": "Hiring 3 SDRs; outbound push",
                "date": "2026-10-20",
                "url": "https://b.example/jobs",
            },
            {"summary": "Opened Austin office", "date": "2026-09-02", "url": "https://b.example/news"},
        ],
    }
    assert lead["number_source"] == "blitzapi" and lead["live_status"] == "live"


def test_one_record_through_csv_and_through_hubspot_gives_the_same_lead():
    from_csv, from_hubspot = read(CSV, guess_map(CSV)), read(HUBSPOT, guess_map(HUBSPOT))
    for field in ("signals",):
        assert from_csv[field] == from_hubspot[field]
    assert from_csv["profile"]["title"] == from_hubspot["profile"]["title"] == "VP Sales"
    assert from_csv["profile"]["size"] == from_hubspot["profile"]["size"] == "120"


def test_signals_with_mismatched_counts_are_never_paired_by_position():
    raw = {**CSV, "signal dates": "2026-10-20"}
    lead = read(raw, guess_map(raw))
    assert lead["signals"]["aligned"] is False
    assert "items" not in lead["signals"] and lead["signals"]["dates"] == ["2026-10-20"]
    assert signal_urls(lead["signals"]) == ["https://b.example/jobs", "https://b.example/news"]


def test_a_300_property_record_keeps_its_mapped_signal_columns():
    wide = {**{f"hs_property_{i:03d}": "x" * 400 for i in range(300)}, **HUBSPOT}
    lead = read(wide, guess_map(wide))
    assert lead["row"]["signal urls"] == HUBSPOT["signal urls"]
    assert lead["signals"]["aligned"] and len(lead["signals"]["items"]) == 2


def test_bookkeeping_columns_never_reach_a_model():
    row = read(CSV, guess_map(CSV))["row"]
    for column in ("email", "apollo person id", "salesforge contact ids", "record id", "fit evidence"):
        assert column not in row
    assert row["company"] == "Brighterway" and row["title"] == "VP Sales"


def test_a_dead_live_status_is_read_from_the_mapped_column():
    assert read({**CSV, "network status": "DEAD"}, guess_map(CSV))["live_status"] == "dead"
    assert read({**CSV, "network status": ""}, guess_map(CSV))["live_status"] is None


def test_a_corrected_map_is_checked_against_the_file():
    headers = list(CSV)
    assert check_map({"title": {"column": "contact headline", "delimiter": None}}, headers) == []
    errors = check_map(
        {
            "colour": {"column": "title"},
            "industry": {"column": "sector"},
            "signal_urls": {"column": "signal urls", "delimiter": ""},
        },
        headers,
    )
    assert len(errors) == 3
