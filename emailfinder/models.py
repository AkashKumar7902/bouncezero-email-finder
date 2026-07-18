"""Shared vocabulary: enums + dataclasses used across the whole package.

Pure Python, stdlib only. Nothing internal is imported here, so there are no
import cycles. Flags (is_catch_all / is_role / is_disposable / webmail) are kept
as fields SEPARATE from raw SMTP codes so results can be reclassified without
re-probing (research dossier section 5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    """Hunter-style deliverability label, separate from the 0-100 score."""

    DELIVERABLE = "deliverable"
    UNDELIVERABLE = "undeliverable"
    RISKY = "risky"
    UNKNOWN = "unknown"


class Provider(str, Enum):
    """MX-derived provider identity.

    The string ``.value`` MUST equal the audit's provider strings so KB rows
    round-trip losslessly (see ``audit_analysis/analyze_audit.py``).
    """

    MICROSOFT365 = "microsoft365"
    GOOGLE_WORKSPACE = "google_workspace"
    CONSUMER_GMAIL = "consumer_gmail"
    PROOFPOINT = "proofpoint"
    MIMECAST = "mimecast"
    CISCO_IRONPORT = "cisco_ironport"
    BARRACUDA = "barracuda"
    ZOHO = "zoho"
    AMAZON_SES = "amazon_ses"
    YAHOO_AOL = "yahoo_aol"
    OTHER = "other"
    NONE_UNKNOWN = "none_or_unknown"


class VerifyStrategy(str, Enum):
    """Dossier 4.2 reliability class derived from Provider; decides whether an
    SMTP RCPT probe is even informative."""

    PROBE = "probe"
    NO_PROBE = "no_probe"
    PROBE_WITH_CATCHALL_GUARD = "probe_with_catchall_guard"
    NO_PROBE_ACCEPT_ALL = "no_probe_accept_all"


@dataclass
class ParsedName:
    """Structured name after cleaning; ``last is None`` => mononym branch."""

    raw: str
    first: str | None
    last: str | None
    middle: list[str] = field(default_factory=list)
    initials: list[str] = field(default_factory=list)
    is_mononym: bool = False
    extra_tokens: list[str] = field(default_factory=list)


@dataclass
class NameVariant:
    """One normalized/expanded name form.

    ``origin`` in {as_given, formal, nickname, surname_expansion, first_initial,
    mononym, romanization} for provenance.
    """

    first: str | None
    last: str | None
    middle: list[str] = field(default_factory=list)
    initials: list[str] = field(default_factory=list)
    origin: str = "as_given"


@dataclass
class Candidate:
    """A concrete local part with the LITERAL template string + separator that
    produced it (never just the shape family). ``source`` in {kb, global}."""

    local_part: str
    template: str
    separator: str
    shape: str
    prior: float
    source: str
    name_origin: str = "as_given"


@dataclass
class MXInfo:
    """MX hosts sorted ascending by preference.

    ``is_implicit`` is True on A/AAAA fallback (RFC 5321 5.1); ``error`` is
    ``'dns_failure'`` when neither MX nor A/AAAA resolves.
    """

    domain: str
    hosts: list[str] = field(default_factory=list)
    is_implicit: bool = False
    error: str | None = None


@dataclass
class DomainFingerprint:
    """Cached per-domain verdict; ``is_catch_all`` is tri-state (None=unknown)."""

    domain: str
    provider: Provider
    mx: list[str] = field(default_factory=list)
    is_catch_all: bool | None = None
    learned_template: str | None = None
    learned_separator: str | None = None
    last_probed_at: float = 0.0
    flags: dict = field(default_factory=dict)


@dataclass
class SmtpResult:
    """One RCPT outcome.

    ``verdict`` in {valid, invalid, catch_all, retry, non_signal, unknown};
    ``unavailable`` is True on port-25 block/timeout (NEVER invalid).
    """

    code: int | None = None
    enhanced: str | None = None
    verdict: str = "unknown"
    reason: str = ""
    unavailable: bool = False


@dataclass
class ScoredCandidate:
    """Candidate after scoring; ``reasons`` is the human-readable
    'why this guess' trail surfaced in the web popover, CLI, and provenance."""

    candidate: Candidate
    score: int
    status: Status
    is_catch_all: bool = False
    is_role: bool = False
    is_disposable: bool = False
    webmail: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def email(self) -> str:
        return self.candidate.local_part + "@" + getattr(self, "_domain", "")


@dataclass
class FindResult:
    """The single object every surface renders.

    ``verification_mode`` in {none, smtp, provider, verification_unavailable}.
    """

    query: dict
    domain: str | None
    provider: Provider
    strategy: VerifyStrategy
    best: ScoredCandidate | None = None
    alternates: list[ScoredCandidate] = field(default_factory=list)
    mx: MXInfo | None = None
    verification_mode: str = "none"
    provenance_id: str | None = None
    suppressed: bool = False
    notes: list[str] = field(default_factory=list)

    def best_email(self) -> str | None:
        if self.best is None or self.domain is None:
            return None
        return self.best.candidate.local_part + "@" + self.domain


@dataclass
class BounceRow:
    """One parsed row from a bounced/audit CSV or DSN, normalized for the
    re-scorer."""

    raw: dict
    email: str
    local: str
    domain: str
    smtp_code: int | None = None
    enhanced: str | None = None
    reason_class: str | None = None
    provider_hint: str | None = None


@dataclass
class FixItem:
    """One re-scorer output row.

    ``verdict`` in {WRONG_GUESS, PROBABLE_INVALID_M365, SENDER_SIDE,
    DOMAIN_ISSUE, TRANSIENT, UNKNOWN}; ``action`` in {bank_known_bad,
    probable_invalid, sender_side_skip, circuit_break, retry_soft}.
    """

    email: str
    domain: str
    verdict: str
    enhanced: str | None = None
    action: str = ""
    corrected_candidate: str | None = None
    kb_change: str | None = None
    detail: str = ""
