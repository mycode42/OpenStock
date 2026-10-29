"""Structured results returned by trade risk validation."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class RejectionReason:
    """A failed risk rule and its human-readable explanation."""

    rule: str
    message: str


@dataclass
class ValidationResult:
    """Result of validating a proposed trade."""

    approved: bool
    reason: str
    rule: Optional[str] = None


__all__ = ["RejectionReason", "ValidationResult"]
