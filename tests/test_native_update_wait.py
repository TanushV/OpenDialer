import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "native_update", Path(__file__).resolve().parents[1] / "scripts" / "native_update.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


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
    assert module.update_installed_or_window_closed(driver) is expected
