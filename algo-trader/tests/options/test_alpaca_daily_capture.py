"""Run-log lines written by the daily Alpaca capture job."""

import re

from scripts.options import alpaca_daily_capture as capture


def test_say_appends_timestamped_line_and_prints(tmp_path, capsys):
    log_file = tmp_path / "options-opening-capture.log"

    capture._say("Alpaca options open capture ok for 2026-11-06: captured 117 of 117, failed 0.", log_file)
    capture._say("Alpaca opening capture skipped: wait until 2026-11-09T09:40:00-05:00.", log_file)

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-\d{2}:\d{2} Alpaca options open capture ok", lines[0])
    assert lines[1].endswith("wait until 2026-11-09T09:40:00-05:00.")
    assert "captured 117 of 117" in capsys.readouterr().out


def test_say_without_log_file_only_prints(tmp_path, capsys):
    capture._say("Alpaca options history capture skipped: 2026-11-07 is not a market session.")

    assert "not a market session" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_select_contracts_keeps_every_strike_inside_the_dte_window():
    from datetime import date
    from scripts.options import alpaca_daily_capture as capture

    session = date(2026, 11, 10)
    rows = [
        {"expiration_date": "2026-12-18", "strike_price": float(strike), "type": kind}
        for strike in range(50, 900)
        for kind in ("call", "put")
    ] + [{"expiration_date": "2027-06-18", "strike_price": 400.0, "type": "call"}]  # beyond 60 DTE
    selected = capture._select_contracts(rows, spot=755.0, session_date=session)
    assert len(selected) == 2 * 850
    assert {row["expiration_date"] for row in selected} == {"2026-12-18"}
