"""BounceZero — an offline-first, provider-aware LinkedIn email finder + verifier.

Public API (stable):

    from emailfinder import find, Engine, Config, load_config, rescore_csv
    from emailfinder import Result, Status

``find`` / ``Engine`` / ``rescore_csv`` are resolved lazily (PEP 562) so that
importing a leaf module (e.g. ``emailfinder.models``) never forces the heavy
orchestration modules to import — this keeps the pure core testable in isolation.
"""
from __future__ import annotations

from .config import Config, ProviderConfig, ScoreConfig, load_config
from .models import FindResult as Result
from .models import Status

__version__ = "0.1.0"

__all__ = [
    "find",
    "Engine",
    "Config",
    "ProviderConfig",
    "ScoreConfig",
    "load_config",
    "rescore_csv",
    "Result",
    "Status",
    "__version__",
]

_default_engine = None


def _get_default_engine():
    global _default_engine
    if _default_engine is None:
        from .engine import Engine

        _default_engine = Engine(load_config())
    return _default_engine


def find(
    name: str | None,
    domain: str | None = None,
    *,
    linkedin_url: str | None = None,
    company: str | None = None,
    user_id: str = "default",
    verify: bool = False,
    use_providers: bool = False,
    config: "Config | None" = None,
):
    """Convenience wrapper: build/reuse a default Engine and delegate to ``find``."""
    if config is not None or user_id != "default":
        from .engine import Engine

        eng = Engine(config or load_config(overrides={"user_id": user_id}))
    else:
        eng = _get_default_engine()
    return eng.find(
        name,
        domain,
        company=company,
        linkedin_url=linkedin_url,
        verify=verify,
        use_providers=use_providers,
    )


def __getattr__(name: str):
    # Lazy access to orchestration symbols (PEP 562).
    if name == "Engine":
        from .engine import Engine

        return Engine
    if name == "rescore_csv":
        from .rescore import rescore_csv

        return rescore_csv
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
