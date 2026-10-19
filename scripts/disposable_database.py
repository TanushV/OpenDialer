"""Destructive test-store admission. This module is never packaged in the app."""

import os

from sqlalchemy.engine import make_url


def validate_database_url(value: str, *, purpose: str = "unit") -> str:
    """Reject everything except the exact, separately named disposable identities."""
    if purpose not in {"unit", "browser"}:
        raise ValueError("Unknown disposable database purpose")
    try:
        url = make_url(value)
    except Exception as exc:
        raise ValueError("Invalid disposable database URL") from exc
    local_name = "opendialer_remediation_test" if purpose == "unit" else "opendialer_completion_e2e"
    local = (
        url.drivername == "postgresql+psycopg"
        and url.host == "127.0.0.1"
        and url.port == 55439
        and url.username == "opendialer"
        and url.password == "remediation-disposable-test"  # noqa: S105 - disposable fixture identity
        and url.database == local_name
        and not url.query
    )
    ci_name = "opendialer_test" if purpose == "unit" else "opendialer_completion_e2e"
    ci = (
        os.getenv("GITHUB_ACTIONS") == "true"
        and url.drivername == "postgresql+psycopg"
        and url.host in {"localhost", "127.0.0.1"}
        and url.port == 5432
        and url.username == "postgres"
        and url.password == "disposable_ci_test"  # noqa: S105 - disposable CI identity
        and url.database == ci_name
        and not url.query
    )
    if not (local or ci):
        # Do not leak possibly real credentials in the refusal.
        raise ValueError(f"Refusing non-disposable {purpose} database")
    return value


def reset_database(value: str, *, purpose: str = "unit") -> None:
    """Reset only the explicitly admitted store, before application startup runs."""
    from app.db import Base
    from sqlalchemy import create_engine

    engine = create_engine(validate_database_url(value, purpose=purpose))
    try:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
