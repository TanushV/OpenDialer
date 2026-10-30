"""Disposable test encryption key; tests never access the user's credential store."""

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(scope="session", autouse=True)
def test_secret_key():
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("OPENDIALER_SECRET_KEY", Fernet.generate_key().decode())
        patch.delenv("OPENDIALER_SECRET_KEY_FILE", raising=False)
        yield
