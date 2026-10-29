import pytest


@pytest.fixture(autouse=True)
def _journal_database_in_tmp(tmp_path, monkeypatch):
    # Journal writes never touch the live state database during tests.
    from reporting import journal_store

    monkeypatch.setattr(journal_store, "DEFAULT_DATABASE", tmp_path / "journal-test.sqlite3")


def write_journal(database, text: str) -> None:
    """Load JSON-lines journal rows into a test state database (the journal's only store)."""
    import json as _json
    from datetime import datetime as _datetime
    from datetime import timezone as _timezone
    from pathlib import Path as _Path

    from reporting import journal_store

    journal_store._connect(_Path(database)).close()  # the database exists even when empty
    rows = [_json.loads(line) for line in text.splitlines() if line.strip()]
    journal_store.append_entries(_Path(database), rows, recorded_at=_datetime.now(_timezone.utc).isoformat())
