from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(slots=True)
class ScanFinding:
    category: str
    value: str


API_KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore (all|previous) instructions", re.IGNORECASE),
    re.compile(r"system prompt", re.IGNORECASE),
    re.compile(r"developer message", re.IGNORECASE),
    re.compile(r"<\|system\|>|\[INST\]|\[/INST\]", re.IGNORECASE),
]

SSRF_DENY_HOSTS = {"localhost", "127.0.0.1"}
SSRF_SCHEMES = {"file", "gopher"}
INTERNAL_IP_PREFIXES = ("10.", "192.168.", "169.254.", "127.")


def _is_luhn_valid(value: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", value)]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, digit in enumerate(digits):
        if i % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def is_luhn_valid(value: str) -> bool:
    return _is_luhn_valid(value)


def is_probable_card_number(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 13 or len(digits) > 19:
        return False
    if len(set(digits)) == 1:
        return False
    if not is_luhn_valid(digits):
        return False
    # Require known issuer ranges to reduce false positives from IDs/timestamps.
    if digits.startswith("4") and len(digits) in {13, 16, 19}:  # Visa
        return True
    prefix2 = int(digits[:2]) if len(digits) >= 2 else -1
    prefix3 = int(digits[:3]) if len(digits) >= 3 else -1
    prefix4 = int(digits[:4]) if len(digits) >= 4 else -1
    prefix6 = int(digits[:6]) if len(digits) >= 6 else -1
    if len(digits) == 16 and (51 <= prefix2 <= 55 or 2221 <= prefix4 <= 2720):  # Mastercard
        return True
    if len(digits) == 15 and prefix2 in {34, 37}:  # AmEx
        return True
    if len(digits) == 16 and (digits.startswith("6011") or digits.startswith("65") or 644 <= prefix3 <= 649):  # Discover
        return True
    if len(digits) == 14 and (300 <= prefix3 <= 305 or prefix2 in {36, 38, 39}):  # Diners Club
        return True
    if 16 <= len(digits) <= 19 and digits.startswith("35"):  # JCB
        return True
    if len(digits) in {16, 19} and (digits.startswith("636") or digits.startswith("637") or digits.startswith("638") or digits.startswith("639")):  # Instapayment-ish
        return True
    if len(digits) in {16, 19} and (digits.startswith("50") or 56 <= prefix2 <= 69):  # Maestro-ish
        return True
    if len(digits) == 16 and 2200 <= prefix4 <= 2204:  # MIR
        return True
    if len(digits) in {16, 19} and digits.startswith("62"):  # UnionPay
        return True
    return False


def scan_text(text: str) -> list[ScanFinding]:
    findings: list[ScanFinding] = []

    for pattern in API_KEY_PATTERNS:
        findings.extend(ScanFinding(category="secret", value=m.group(0)) for m in pattern.finditer(text))

    findings.extend(ScanFinding(category="secret", value=m.group(0)) for m in JWT_PATTERN.finditer(text))
    findings.extend(ScanFinding(category="pii_email", value=m.group(0)) for m in EMAIL_PATTERN.finditer(text))
    findings.extend(ScanFinding(category="pii_phone", value=m.group(0)) for m in PHONE_PATTERN.finditer(text))
    findings.extend(ScanFinding(category="pii_ssn", value=m.group(0)) for m in SSN_PATTERN.finditer(text))

    for match in CARD_PATTERN.finditer(text):
        if is_probable_card_number(match.group(0)):
            findings.append(ScanFinding(category="pii_card", value=match.group(0)))

    for pattern in PROMPT_INJECTION_PATTERNS:
        findings.extend(ScanFinding(category="prompt_injection", value=m.group(0)) for m in pattern.finditer(text))

    for token in re.findall(r"https?://\S+|file://\S+|gopher://\S+", text):
        if is_ssrf_risky_url(token):
            findings.append(ScanFinding(category="ssrf", value=token))

    return findings


def is_ssrf_risky_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme in SSRF_SCHEMES:
        return True

    host = (parsed.hostname or "").lower()
    if host in SSRF_DENY_HOSTS:
        return True
    return host.startswith(INTERNAL_IP_PREFIXES)
