"""E00: a test typo must never reset an installed or user database."""

import pytest

from scripts.disposable_database import validate_database_url

LOCAL = "postgresql+psycopg://opendialer:remediation-disposable-test@127.0.0.1:55439/"


@pytest.mark.parametrize(
    "purpose,name", [("unit", "opendialer_remediation_test"), ("browser", "opendialer_completion_e2e")]
)
def test_exact_disposable_identity_is_admitted(purpose, name):
    assert validate_database_url(LOCAL + name, purpose=purpose) == LOCAL + name


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:////Users/example/Library/Application Support/OpenDialer/local.db",
        LOCAL + "production",
        LOCAL + "opendialer_completion_e2e",
        LOCAL.replace("127.0.0.1", "example.com") + "opendialer_remediation_test",
        LOCAL.replace("55439", "54322") + "opendialer_remediation_test",
        LOCAL.replace("remediation-disposable-test", "real-secret") + "opendialer_remediation_test",
        LOCAL + "opendialer_remediation_test?options=-csearch_path%3Dproduction",
        "not-a-database",
    ],
)
def test_other_databases_are_refused_without_credentials_in_error(url):
    with pytest.raises(ValueError) as error:
        validate_database_url(url)
    assert "real-secret" not in str(error.value)


def test_ci_identity_requires_actual_ci_and_distinct_browser_database(monkeypatch):
    url = "postgresql+psycopg://postgres:disposable_ci_test@localhost:5432/opendialer_test"
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError):
        validate_database_url(url)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert validate_database_url(url) == url
    with pytest.raises(ValueError):
        validate_database_url(url, purpose="browser")


def test_unit_and_browser_stores_cannot_alias():
    with pytest.raises(ValueError):
        validate_database_url(LOCAL + "opendialer_remediation_test", purpose="browser")


def test_reset_refuses_foreign_store_before_opening_connection(monkeypatch):
    import sqlalchemy

    from scripts.disposable_database import reset_database

    opened = []
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: opened.append(a))
    with pytest.raises(ValueError):
        reset_database(LOCAL + "production")
    assert opened == []
