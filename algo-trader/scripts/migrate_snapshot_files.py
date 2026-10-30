"""Move file-based account snapshots into the algo state database.

Covers the Algo account (data/snapshots/) and the options paper account
(data/options/accounts/<digest>/snapshots/). Rows are inserted with INSERT OR IGNORE,
so re-running is safe. Every record is checked against the table afterwards.

Without --delete the script only imports and verifies. With --delete it removes a
directory's JSON and JSONL files, but only when every record in them is readable and
stored. Run it with the environment loaded, e.g. from /opt/OpenStock/.env.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from contextlib import closing
from pathlib import Path

from reporting import snapshot_store
from runtime.paths import DATA_DIR


def _options_directory(account_id: str) -> Path:
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
    return DATA_DIR / "options" / "accounts" / digest / "snapshots"


def _remove_files(directory: Path) -> int:
    removed = 0
    for path in list(directory.glob("*.json")) + list((directory / "intraday").glob("*.jsonl")):
        path.unlink()
        removed += 1
    intraday = directory / "intraday"
    if intraday.exists() and not any(intraday.iterdir()):
        shutil.rmtree(intraday)
    return removed


def migrate(directory: Path, *, account_id: str | None, delete: bool) -> bool:
    records, unreadable = snapshot_store.read_files(directory)
    invalid = 0
    inserted = 0
    valid: list[dict] = []
    with closing(snapshot_store.connect()) as connection:
        for record in records:
            key_account = account_id or str(record.get("account_id") or "")
            try:
                inserted += snapshot_store.insert(
                    connection, record, trading_mode=str(record.get("trading_mode")), account_id=key_account,
                )
                valid.append(record)
            except (KeyError, TypeError, ValueError):
                invalid += 1
        missing = snapshot_store.missing_from_store(connection, valid, account_id=account_id)
    print(
        f"{directory}: files readable records={len(records)} inserted={inserted} "
        f"already_stored={len(valid) - inserted} invalid={invalid} unreadable_files={unreadable} missing={missing}"
    )
    verified = unreadable == 0 and invalid == 0 and missing == 0
    if delete and verified and records:
        removed = _remove_files(directory)
        print(f"{directory}: removed {removed} file(s)")
    elif delete and not verified:
        print(f"{directory}: not deleted; verification failed")
    return verified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delete", action="store_true", help="remove the JSON files once they are verified")
    args = parser.parse_args()

    ok = migrate(DATA_DIR / "snapshots", account_id=None, delete=args.delete)

    options_account = os.environ.get("ALPACA_OPTIONS_ACCOUNT_ID", "").strip()
    if options_account:
        options_directory = _options_directory(options_account)
        if options_directory.exists():
            ok = migrate(options_directory, account_id=options_account, delete=args.delete) and ok
        else:
            print(f"{options_directory}: no options snapshot directory")
    else:
        print("ALPACA_OPTIONS_ACCOUNT_ID is not set; options snapshots were not migrated", file=sys.stderr)
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
