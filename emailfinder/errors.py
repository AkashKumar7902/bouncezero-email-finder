"""Typed exception hierarchy for provider adapters + engine (dossier 6.3).

The registry/engine branch on the error *kind* (fail-fast vs skip vs retry vs
circuit-break) instead of parsing message strings.
"""
from __future__ import annotations


class ProviderError(Exception):
    """Base class for all paid-provider adapter failures."""


class ErrAuth(ProviderError):
    """Bad/expired API key -> disable the provider for the session."""


class ErrQuotaExhausted(ProviderError):
    """Out of credits -> skip this provider until the next billing tick."""


class ErrRateLimited(ProviderError):
    """Hit a rate limit -> back off. Carries ``retry_after`` seconds."""

    def __init__(self, message: str = "", retry_after: float = 60.0):
        super().__init__(message)
        self.retry_after = retry_after


class ErrTimeout(ProviderError):
    """Request timed out -> retry once, then give up."""


class ErrProviderDown(ProviderError):
    """5xx / connection failure -> circuit-break this provider for ~5 min."""


class ErrBadInput(ProviderError):
    """Request payload is invalid (non-retryable)."""


class ComplianceBlocked(Exception):
    """Query target is on the global suppression list.

    Handled internally; the engine converts it to ``FindResult(suppressed=True)``.
    """
