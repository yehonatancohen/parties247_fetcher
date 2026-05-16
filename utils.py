"""Shared URL and slug utilities (self-contained, no app dependency)."""

import re
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}

_HE_TO_LATIN = {
    'א': 'a', 'ב': 'b', 'ג': 'g', 'ד': 'd', 'ה': 'h', 'ו': 'v',
    'ז': 'z', 'ח': 'kh', 'ט': 't', 'י': 'y', 'כ': 'k', 'ך': 'k',
    'ל': 'l', 'מ': 'm', 'ם': 'm', 'נ': 'n', 'ן': 'n', 'ס': 's',
    'ע': 'a', 'פ': 'p', 'ף': 'p', 'צ': 'ts', 'ץ': 'ts', 'ק': 'k',
    'ר': 'r', 'ש': 'sh', 'ת': 't',
}


def _transliterate_hebrew(text: str) -> str:
    return ''.join(_HE_TO_LATIN.get(c, c) for c in text)


def slugify_value(value: str | None) -> str | None:
    if not value:
        return None
    value = _transliterate_hebrew(value)
    slug = re.sub(r"[^0-9a-zA-Z]+", "-", value.lower()).strip("-")
    return slug or None


def slugify_party(name: str | None, date_str: str | None) -> str | None:
    base = slugify_value(name)
    if not base:
        return None
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            month = dt.strftime("%B").lower()
            year = str(dt.year)
            base = f"{base}-{month}-{year}"
        except Exception:
            pass
    if len(base) > 80:
        base = base[:80].rsplit("-", 1)[0]
    return base or None


def normalize_url(raw: str) -> str:
    p = urlparse((raw or "").strip())
    scheme = (p.scheme or "https").lower()
    netloc = p.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = p.path.rstrip("/") or "/"
    cleaned_qs = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith(TRACKING_PREFIXES) or kl in TRACKING_KEYS:
            continue
        cleaned_qs.append((k, v))
    query = urlencode(cleaned_qs, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def normalized_or_none_for_dedupe(raw: str) -> str | None:
    n = normalize_url(raw)
    return n if urlparse(n).netloc else None


def is_url_allowed(raw: str) -> bool:
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    if parsed.port and parsed.port not in ALLOWED_PORTS:
        return False
    host = parsed.hostname or ""
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return False
    return True


def append_referral_param(url: str | None, referral: str | None) -> str | None:
    if not url or not referral:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    if not parsed.netloc:
        return url
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if any(k.lower() == "ref" for k, _ in query_items):
        return urlunparse(parsed)
    query_items.append(("ref", referral))
    new_query = urlencode(query_items, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def apply_default_referral(party: dict, referral: str | None) -> None:
    if not party or not referral:
        return
    for key in ("goOutUrl", "originalUrl"):
        if key in party:
            party[key] = append_referral_param(party.get(key), referral)
    if not party.get("referralCode"):
        party["referralCode"] = referral
