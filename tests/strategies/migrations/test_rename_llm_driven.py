"""The LLM strategy ID rename rewrites state copies only and is idempotent."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
import sqlite3

import pytest

from strategies.migrations import rename_llm_driven as migration


NEW = "swing_llm_driven"
OLD = "swing_event_driven"

SCHEMA = """
CREATE TABLE coordinator (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    market_state TEXT NOT NULL,
    state_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_at TEXT NOT NULL
);
CREATE TABLE strategy_state (
    strategy TEXT NOT NULL,
    ticker TEXT NOT NULL,
    state_json TEXT NOT NULL,
    PRIMARY KEY (strategy, ticker)
);
CREATE TABLE position_owner (
    ticker TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    FOREIGN KEY (strategy, ticker) REFERENCES strategy_state (strategy, ticker)
);
CREATE TABLE entry_provenance (
    order_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    client_order_id TEXT,
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


def _state_json(strategy: str, **extra) -> str:
    return json.dumps({"owner": strategy, "strategy": strategy, "phase": "open", **extra},
                      sort_keys=True, separators=(",", ":"))


def _make_state(path: Path, *, conflict: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO coordinator VALUES (1, 'open', ?, 1, '2026-10-27T00:00:00Z')",
        (json.dumps({"active_strategies": [OLD, "swing_momentum"], "last_signals": []}),),
    )
    connection.execute(
        "INSERT INTO strategy_state VALUES (?, 'TSM', ?)",
        (OLD, _state_json(OLD, research_profile_id=f"{OLD}:range_bound")),
    )
    connection.execute(
        "INSERT INTO strategy_state VALUES (?, 'AAPL', ?)",
        ("swing_momentum", _state_json("swing_momentum")),
    )
    connection.execute("INSERT INTO position_owner VALUES ('TSM', ?)", (OLD,))
    connection.execute("INSERT INTO position_owner VALUES ('AAPL', 'swing_momentum')")
    connection.execute(
        "INSERT INTO entry_provenance VALUES ('order-1', 'TSM', ?, 'rule-1', 'rule', '2026-10-20T14:00:00Z')",
        (OLD,),
    )
    if conflict:
        connection.execute(
            "INSERT INTO strategy_state VALUES (?, 'TSM', ?)", (NEW, _state_json(NEW)),
        )
    connection.commit()
    connection.close()


JOURNAL_LINES = [
    json.dumps({"ticker": "TSM", "strategy": OLD, "action": "buy", "reasoning": "news"}) + "\n",
    json.dumps({"ticker": "AMZN", "strategy": "event_driven", "action": "sell"}) + "\n",
    json.dumps({"ticker": "AAPL", "strategy": "swing_momentum", "reasoning": f"mentions {OLD} in text"}) + "\n",
    "{not json but mentions " + OLD + "\n",
]


def _make_journal(path: Path) -> None:
    path.write_text("".join(JOURNAL_LINES), encoding="utf-8")


def _rows(path: Path, query: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


@pytest.fixture
def copies(tmp_path):
    state = tmp_path / "algo_state.sqlite3"
    journal = tmp_path / "trade_journal.jsonl"
    backups = tmp_path / "backups"
    _make_state(state)
    _make_journal(journal)
    return state, journal, backups


def test_rewrite_value_changes_exact_ids_and_profile_prefixes_only():
    counts: dict[str, int] = {}
    value = {
        OLD: 1,
        "strategy": OLD,
        "profile": f"{OLD}:range_bound",
        "text": f"mentions {OLD} in prose",
        "nested": [OLD, {"owner": "event_driven"}],
    }
    result = migration.rewrite_value(value, counts)

    assert result == {
        NEW: 1,
        "strategy": NEW,
        "profile": f"{NEW}:range_bound",
        "text": f"mentions {OLD} in prose",
        "nested": [NEW, {"owner": NEW}],
    }
    assert counts["values"] == 5


def test_dry_run_reports_counts_and_writes_nothing(copies):
    state, journal, backups = copies
    before_state = state.read_bytes()
    before_journal = journal.read_bytes()

    report = migration.run(
        state_db=state, journal=journal, apply=False, backup_root=backups, lock_file=None,
    )

    assert report["mode"] == "dry_run"
    assert report["counts"] == {
        "strategy_state_rows": 1, "position_owner_rows": 1, "entry_provenance_rows": 1,
        "coordinator_rows": 1, "journal_lines": 2,
    }
    assert report["journal_unparsed_with_old_id"] == 1
    assert state.read_bytes() == before_state
    assert journal.read_bytes() == before_journal
    assert not backups.exists()


def test_apply_rewrites_state_and_journal_and_backs_up_first(copies):
    state, journal, backups = copies
    original_journal = journal.read_text(encoding="utf-8")

    report = migration.run(
        state_db=state, journal=journal, apply=True, backup_root=backups, lock_file=None,
    )

    assert report["mode"] == "apply"
    backup_dir = Path(report["backup_dir"])
    assert backup_dir.parent == backups and backup_dir.name.startswith("llm_driven_rename-")
    assert _rows(backup_dir / state.name, "SELECT ticker, strategy FROM strategy_state ORDER BY ticker") == [
        ("AAPL", "swing_momentum"), ("TSM", OLD),
    ]
    assert (backup_dir / journal.name).read_text(encoding="utf-8") == original_journal

    assert _rows(state, "SELECT ticker, strategy FROM strategy_state ORDER BY ticker") == [
        ("AAPL", "swing_momentum"), ("TSM", NEW),
    ]
    tsm_json = _rows(state, "SELECT state_json FROM strategy_state WHERE ticker = 'TSM'")[0][0]
    assert json.loads(tsm_json)["strategy"] == NEW
    assert json.loads(tsm_json)["owner"] == NEW
    assert json.loads(tsm_json)["research_profile_id"] == f"{NEW}:range_bound"
    assert _rows(state, "SELECT ticker, strategy FROM position_owner ORDER BY ticker") == [
        ("AAPL", "swing_momentum"), ("TSM", NEW),
    ]
    assert _rows(state, "SELECT strategy FROM entry_provenance") == [(NEW,)]
    coordinator = json.loads(_rows(state, "SELECT state_json FROM coordinator")[0][0])
    assert coordinator["active_strategies"] == [NEW, "swing_momentum"]

    lines = journal.read_text(encoding="utf-8").splitlines(keepends=True)
    original_lines = original_journal.splitlines(keepends=True)
    assert json.loads(lines[0])["strategy"] == NEW
    assert json.loads(lines[1])["strategy"] == NEW
    assert lines[2] == original_lines[2]
    assert lines[3] == original_lines[3]
    assert report["state_written"] == {
        "strategy_state": 1, "position_owner": 1, "entry_provenance": 1, "coordinator": 1,
    }
    assert report["journal_written"] == 2


def test_second_apply_is_a_no_op_and_takes_no_new_backup(copies):
    state, journal, backups = copies
    migration.run(state_db=state, journal=journal, apply=True, backup_root=backups, lock_file=None)
    state_after = state.read_bytes()
    journal_after = journal.read_bytes()
    backup_count = len(list(backups.iterdir()))

    report = migration.run(
        state_db=state, journal=journal, apply=True, backup_root=backups, lock_file=None,
    )

    assert report["mode"] == "apply_nothing_to_do"
    assert all(value == 0 for value in report["counts"].values())
    assert state.read_bytes() == state_after
    assert journal.read_bytes() == journal_after
    assert len(list(backups.iterdir())) == backup_count


def test_conflicting_rows_refuse_before_any_write(tmp_path):
    state = tmp_path / "algo_state.sqlite3"
    journal = tmp_path / "trade_journal.jsonl"
    backups = tmp_path / "backups"
    _make_state(state, conflict=True)
    _make_journal(journal)
    before_state = state.read_bytes()
    before_journal = journal.read_bytes()

    with pytest.raises(migration.MigrationConflict, match="already has"):
        migration.run(state_db=state, journal=journal, apply=True, backup_root=backups, lock_file=None)

    assert state.read_bytes() == before_state
    assert journal.read_bytes() == before_journal
    assert not backups.exists()


def test_running_daemon_lock_blocks_apply(copies, tmp_path):
    state, journal, backups = copies
    lock_path = tmp_path / "algo-trader.lock"
    lock_path.touch()
    before_state = state.read_bytes()
    holder = open(lock_path, "r")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(RuntimeError, match="stop it before --apply"):
            migration.run(
                state_db=state, journal=journal, apply=True, backup_root=backups, lock_file=lock_path,
            )
    finally:
        holder.close()

    assert state.read_bytes() == before_state
    assert not backups.exists()


def test_cli_dry_run_prints_a_json_report(copies, capsys):
    state, journal, backups = copies
    code = migration.main([
        "--state-db", str(state), "--journal", str(journal),
        "--backup-root", str(backups), "--no-lock-check",
    ])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry_run"
    assert report["new_id"] == NEW


def test_free_text_diagnostics_are_not_rewritten_or_rewritten_twice(tmp_path):
    state = tmp_path / "algo_state.sqlite3"
    journal = tmp_path / "trade_journal.jsonl"
    backups = tmp_path / "backups"
    connection = sqlite3.connect(state)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO coordinator VALUES (1, 'open', ?, 1, '2026-10-27T00:00:00Z')",
        (json.dumps({"scan_errors": [f"strategy scan failed: KeyError: '{OLD}'"]}),),
    )
    connection.commit()
    connection.close()
    journal.write_text(json.dumps({"strategy": "momentum"}) + "\n", encoding="utf-8")

    report = migration.run(state_db=state, journal=journal, apply=True, backup_root=backups, lock_file=None)

    assert report["mode"] == "apply_nothing_to_do"
    assert report["counts"]["coordinator_rows"] == 0
    assert not backups.exists()
