"""HTTP-only helpers for the feature acceptance suite.

The acceptance suite drives the authority only through its public HTTP API (plus the
simulated carrier and the test clock), so it keeps passing while internals are rewritten.
Never import application modules or read the database from an acceptance test.
"""

from uuid import uuid4

from conftest import disposition_body, ready, reserve, start  # noqa: F401 - re-exported, HTTP only


def call(ws, method, path, status=200, **kwargs):
    """Send one request as the workspace operator and assert its status."""
    headers = {**ws["headers"], **kwargs.pop("headers", {})}
    response = ws["client"].request(method, path, headers=headers, **kwargs)
    assert response.status_code == status, (method, path, response.status_code, response.text)
    return response.json() if response.content else None


def get(ws, path, status=200, **kwargs):
    return call(ws, "GET", path, status, **kwargs)


def post(ws, path, json=None, status=200, **kwargs):
    return call(ws, "POST", path, status, json={} if json is None else json, **kwargs)


def run_jobs(ws, rounds=1):
    """Drain the background lanes through the admin route, as an operator would."""
    receipt = None
    for _ in range(rounds):
        receipt = post(ws, "/v1/admin/jobs/run")
    return receipt


def event(ws, call_id, event_type, event_id=None, **payload):
    """Deliver one simulated carrier event for a call."""
    return post(
        ws,
        "/v1/simulation/event",
        {
            "event_id": event_id or str(uuid4()),
            "event_type": event_type,
            "call_id": call_id,
            "payload": payload,
        },
    )


def answered_by_human(ws, call_id):
    event(ws, call_id, "call.answered")
    event(ws, call_id, "human.confirmed")
    return get(ws, f"/v1/calls/{call_id}")


def hang_up(ws, call_id):
    event(ws, call_id, "call.hangup")
    return get(ws, f"/v1/calls/{call_id}")


def wrap_up(ws, call_id, **values):
    body = disposition_body(ws["client"], ws["headers"], call_id, **values)
    return post(ws, f"/v1/calls/{call_id}/disposition", body)


def import_leads(ws, rows, **extra):
    return post(ws, "/v1/leads/import", {"campaign_id": ws["campaign_id"], "rows": rows, **extra})
