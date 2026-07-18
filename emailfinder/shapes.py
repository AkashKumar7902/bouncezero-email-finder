"""PURE structural classifier for an email local part.

A byte-for-byte port of ``audit_analysis/analyze_audit.py``'s ``shape()`` so
that re-scored KB rows and seed KB rows share ONE taxonomy. Used by kb_store,
rescore and feedback; NOT by candidate generation. Zero I/O, zero deps.

The port MUST stay identical to the audit generator — ``tests/test_shapes.py``
validates it golden against ``emailfinder/data/audit_records.csv``.
"""
from __future__ import annotations

import re

_NAME_DIGITS = re.compile(r"[a-z]+\d+")
_SEPARATORS = ((".", "dot"), ("_", "underscore"), ("-", "hyphen"))


def shape(local: str) -> tuple[str, str]:
    """Return ``(shape_label, separator)`` exactly mirroring the audit generator.

    For each separator in ``. _ -`` (in that order): if present and the local
    splits into exactly 2 tokens, return ``f{sep}last`` (first token is a single
    letter), ``first{sep}l`` (last token is a single letter), else
    ``first{sep}last``; more than 2 tokens -> ``multi{sep}``. Otherwise an
    all-alpha local -> ``('single_token', '')``; an ``[a-z]+\\d+`` local ->
    ``('name+digits', '')``; anything else -> ``('other', '')``.
    """
    for sep, _label in _SEPARATORS:
        if sep in local:
            toks = local.split(sep)
            if len(toks) == 2:
                a, b = toks
                if len(a) == 1 and b.isalpha():
                    return f"f{sep}last", sep
                if len(b) == 1 and a.isalpha():
                    return f"first{sep}l", sep
                return f"first{sep}last", sep
            return f"multi{sep}", sep
    if local.isalpha():
        return "single_token", ""
    if _NAME_DIGITS.fullmatch(local):
        return "name+digits", ""
    return "other", ""
