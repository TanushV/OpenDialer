from types import SimpleNamespace

import pytest

from scripts.native_update import update_installed_or_window_closed


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, True),
        (SimpleNamespace(text=None), False),
        (SimpleNamespace(text="Downloading update"), False),
        (SimpleNamespace(text="Update installed. Quit and reopen."), True),
    ],
)
def test_updater_wait_handles_the_native_window_closing(body, expected):
    driver = SimpleNamespace(find_element=lambda *_args: body)
    assert update_installed_or_window_closed(driver) is expected
