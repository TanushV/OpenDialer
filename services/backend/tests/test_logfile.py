"""R0.13: every backend warning reaches one private, rotating log file in the data folder."""

import logging

from app.logfile import configure


def test_warnings_reach_a_private_rotating_log_file(tmp_path):
    path = configure(tmp_path)
    try:
        logging.getLogger("app.telephony.ingress").warning("relay exchange failed: %s", "TimeoutError")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert path == tmp_path / "logs" / "opendialer.log"
        assert "relay exchange failed: TimeoutError" in path.read_text()
        assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700
        # Configuring again adds no second handler, so no line is written twice.
        configure(tmp_path)
        logging.getLogger("app").warning("once")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert path.read_text().count("once") == 1
    finally:
        configure(None)
