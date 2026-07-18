"""Runtime configuration: defaults <- optional JSON <- env <- explicit overrides.

Holds every tunable numeric constant (scoring weights, confidence caps, cache
TTLs, timeouts), feature flags (SMTP off, providers off), the per-user silo
location, and paid-provider registrations. Fully functional with no config file
at all (zero-provider local mode is the default).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_DATA_DIR = Path.home() / ".emailfinder"


@dataclass
class ScoreConfig:
    """Scoring weights + hard caps. All defaults are engineering judgement, not
    audit-calibrated — tune against held-out bounce outcomes."""

    w_src: float = 0.25   # public-source corroboration of the exact local part
    w_dom: float = 0.35   # KB dominant-pattern match (strongest pattern signal)
    w_pat: float = 0.25   # global pattern prior
    w_smtp: float = 0.15  # SMTP signal (gated by provider)
    catchall_cap: int = 58
    m365_cap: int = 50
    accept_all_cap: int = 50
    kb_match_base: tuple[int, int] = (75, 85)
    global_prior_base: tuple[int, int] = (55, 65)


@dataclass
class ProviderConfig:
    """One paid-provider registration; ``enabled`` defaults False."""

    name: str
    api_key_env: str
    enabled: bool = False
    priority: int = 100
    role: str = "finder"          # finder | verifier | both
    max_credits_per_day: int | None = None


@dataclass
class Config:
    """Whole-app config. SMTP + providers OFF by default; the short
    ``smtp_connect_timeout`` makes a port-25 block detectable fast."""

    data_dir: Path = DEFAULT_DATA_DIR
    user_id: str = "default"
    enable_smtp: bool = False
    enable_providers: bool = False
    smtp_connect_timeout: float = 6.0
    smtp_cmd_timeout: float = 30.0
    mail_from: str | None = None
    ehlo_hostname: str | None = None
    dns_timeout: float = 5.0
    kb_dominance_threshold: float = 0.60
    domain_cache_ttl_days: int = 14
    verify_cache_ttl_days: int = 30
    find_cache_ttl_days: int = 90
    retention_days: int = 90
    enable_smb_size_conditioning: bool = False
    score: ScoreConfig = field(default_factory=ScoreConfig)
    providers: list[ProviderConfig] = field(default_factory=list)
    package_data_dir: Path = PACKAGE_DATA_DIR

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser()
        self.package_data_dir = Path(self.package_data_dir)


def _coerce_paths(cfg: Config) -> Config:
    cfg.data_dir = Path(cfg.data_dir).expanduser()
    cfg.package_data_dir = Path(cfg.package_data_dir)
    return cfg


def load_config(path: Path | None = None, overrides: dict | None = None) -> Config:
    """Layer hardcoded defaults <- optional config.json <- env vars <- overrides.

    Returns an all-defaults Config when ``path`` is None and no env/overrides are
    given. ``data_dir`` resolves to ``~/.emailfinder`` by default.
    """
    cfg = Config()

    # Layer 2: JSON config file
    if path is not None:
        raw = json.loads(Path(path).expanduser().read_text())
        cfg = _apply_dict(cfg, raw)

    # Layer 3: environment variables (EMAILFINDER_*)
    env_map = {
        "EMAILFINDER_DATA_DIR": ("data_dir", Path),
        "EMAILFINDER_USER_ID": ("user_id", str),
        "EMAILFINDER_ENABLE_SMTP": ("enable_smtp", _as_bool),
        "EMAILFINDER_ENABLE_PROVIDERS": ("enable_providers", _as_bool),
        "EMAILFINDER_MAIL_FROM": ("mail_from", str),
        "EMAILFINDER_EHLO_HOSTNAME": ("ehlo_hostname", str),
    }
    for env_key, (attr, conv) in env_map.items():
        if env_key in os.environ and os.environ[env_key] != "":
            setattr(cfg, attr, conv(os.environ[env_key]))

    # Layer 4: explicit overrides
    if overrides:
        cfg = _apply_dict(cfg, overrides)

    return _coerce_paths(cfg)


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _apply_dict(cfg: Config, raw: dict) -> Config:
    """Apply a nested dict onto a Config, handling score/providers specially."""
    kwargs: dict = {}
    for key, value in raw.items():
        if key == "score" and isinstance(value, dict):
            kwargs["score"] = replace(cfg.score, **value)
        elif key == "providers" and isinstance(value, list):
            kwargs["providers"] = [
                ProviderConfig(**p) if isinstance(p, dict) else p for p in value
            ]
        elif key in Config.__dataclass_fields__:
            kwargs[key] = value
    return replace(cfg, **kwargs)
