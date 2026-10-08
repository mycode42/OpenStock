"""A daily, account-bound receipt for Day's verified opening cleanup.

Day entries are allowed only after a live liquidation check has confirmed
that this account started the session without positions or open orders.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("America/New_York")
RECOVERY_PATH = Path(__file__).resolve().parents[1] / "data" / "day_recovery.json"


def record_day_recovery(account_id: str, now: datetime | None = None) -> dict:
    """Atomically record a broker-verified flat account for this ET date."""
    if not account_id or account_id == "unknown":
        raise ValueError("Day account ID is not configured")
    local_now = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
    receipt = {
        "trading_date": local_now.date().isoformat(),
        "verified_flat_at": local_now.isoformat(),
        "account_id": account_id,
        "verified_flat": True,
    }
    RECOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".day-recovery-", dir=RECOVERY_PATH.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(receipt, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, RECOVERY_PATH)
        os.chmod(RECOVERY_PATH, 0o600)
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise
    return receipt


def day_recovery_confirmed(
    account_id: str,
    now: datetime | None = None,
    *,
    session_open_at: datetime | None = None,
) -> bool:
    """Require this account's flat receipt after today's broker session open."""
    if not account_id or account_id == "unknown" or session_open_at is None or session_open_at.tzinfo is None:
        return False
    local_now = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
    local_open = session_open_at.astimezone(MARKET_TZ)
    if local_open.date() != local_now.date() or local_now < local_open:
        return False
    try:
        receipt = json.loads(RECOVERY_PATH.read_text())
        verified_at = datetime.fromisoformat(receipt["verified_flat_at"])
        return bool(
            receipt.get("verified_flat") is True
            and receipt.get("account_id") == account_id
            and receipt.get("trading_date") == local_now.date().isoformat()
            and verified_at.tzinfo is not None
            and verified_at.astimezone(MARKET_TZ).date() == local_now.date()
            and verified_at >= local_open
            and verified_at <= local_now
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
