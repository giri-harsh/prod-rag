"""PII / secret redaction for TELEMETRY (Logfire spans, python logging, LangSmith). Never applied to API answers."""
from __future__ import annotations

import logging
import re
from typing import Any

REPLACEMENT = "[REDACTED:{}]"

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE = re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,5}[\s.-]\d{3,5}(?:[\s.-]\d{2,5})?(?![\w.])|\+\d{10,14}\b")
_AADHAAR = re.compile(r"(?<!\d)[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_KEY = re.compile(
    r"\b(?:gsk_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16}|rag_[A-Za-z0-9_-]{20,}"
    r"|lsv2_[A-Za-z0-9_]{16,}|pylf_[A-Za-z0-9_]{10,}|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,})"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{20,}"
)
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{1,4}\b")

# (label, pattern) — order matters: secrets first so they are not mis-tagged as phone/card digits
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("KEY", _KEY), ("EMAIL", _EMAIL), ("PAN", _PAN), ("AADHAAR", _AADHAAR), ("CARD", _CARD),
    ("IP", _IPV4), ("IP", _IPV6), ("PHONE", _PHONE),
]


def luhn_ok(digits: str) -> bool:
    ds = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(ds) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2 == 1:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def redact(text: str) -> str:
    for label, rx in PATTERNS:
        if label == "CARD":
            text = rx.sub(lambda m: REPLACEMENT.format("CARD") if luhn_ok(m.group(0)) else m.group(0), text)
        elif label == "AADHAAR":
            text = rx.sub(lambda m: REPLACEMENT.format("AADHAAR") if len(re.sub(r"\D", "", m.group(0))) == 12 else m.group(0), text)
        else:
            text = rx.sub(REPLACEMENT.format(label), text)
    return text


def redact_obj(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact strings inside dicts/lists/tuples (LangSmith hide_inputs/hide_outputs hook)."""
    if _depth > 12:
        return obj
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, _depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v, _depth + 1) for v in obj)
    return obj


class PIIFilter(logging.Filter):
    """Redacts the formatted message of every record passing through a handler/logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, None
        return True


def logfire_scrubbing():
    """Logfire ScrubbingOptions: our PII patterns are scanned in attribute values; matches are redacted in place."""
    import logfire

    extra = [p.pattern for _, p in PATTERNS]
    ours = set(extra)

    def callback(m: "logfire.ScrubMatch"):  # type: ignore[name-defined]
        if m.pattern_match.re.pattern in ours:
            return redact(m.value) if isinstance(m.value, str) else REPLACEMENT.format("PII")
        return None  # default Logfire handling for its own sensitive-key patterns

    return logfire.ScrubbingOptions(extra_patterns=extra, callback=callback)


def install_logging_filter() -> None:
    root = logging.getLogger()
    if not any(isinstance(f, PIIFilter) for f in root.filters):
        root.addFilter(PIIFilter())
    for h in root.handlers:
        if not any(isinstance(f, PIIFilter) for f in h.filters):
            h.addFilter(PIIFilter())


def langsmith_client():
    """LangSmith client whose inputs/outputs are redacted before leaving the process."""
    from langsmith import Client

    return Client(hide_inputs=redact_obj, hide_outputs=redact_obj)
