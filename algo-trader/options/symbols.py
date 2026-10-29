"""OCC option-symbol parsing for broker positions and audit logs."""

from __future__ import annotations

from datetime import date
import re

from options.models import OptionContract


_OCC = re.compile(r"^(?P<root>[A-Z0-9.]{1,6})(?P<expiry>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")


def parse_occ_symbol(symbol: str, *, multiplier: int = 100, exercise_style: str = "american") -> OptionContract:
    """Parse standard OCC root + YYMMDD + right + strike/1000 notation."""
    normalized = symbol.strip().upper().replace(" ", "")
    match = _OCC.fullmatch(normalized)
    if not match:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    raw = match.group("expiry")
    expiration = date(2000 + int(raw[:2]), int(raw[2:4]), int(raw[4:6]))
    root = match.group("root").rstrip()
    return OptionContract(
        symbol=normalized,
        underlying=root,
        expiration=expiration,
        right="call" if match.group("right") == "C" else "put",
        strike=int(match.group("strike")) / 1000.0,
        multiplier=multiplier,
        exercise_style=exercise_style,
    )


__all__ = ["parse_occ_symbol"]
