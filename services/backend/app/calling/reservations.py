"""The durable claim shared by prospect and test dials."""

from datetime import timedelta

from app.platform.db import ActiveClaim, scope_of, scoped_put, workspace


def hold(s, p, recipient, number, metadata, *, now):
    expires = now + timedelta(minutes=2)
    reservation = scoped_put(
        s,
        p,
        "reservations",
        {
            "rep_id": p["user_id"],
            "lead_id": recipient.id,
            "endpoint_id": recipient.data["endpoint_id"],
            "number_id": number.id,
            "expires_at": expires.isoformat(),
            "status": "reserved",
            **metadata,
        },
        now=now,
    )
    s.add(
        ActiveClaim(
            **scope_of(workspace(p)),
            seller_id=p["seller_id"],
            endpoint_id=recipient.data["endpoint_id"],
            lead_id=recipient.id,
            rep_id=p["user_id"],
            number_id=number.id,
            reservation_id=reservation.id,
            expires_at=expires,
        )
    )
    return reservation
