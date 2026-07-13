"""
Auto-assign parties to existing carousels based on title keyword heuristics.

Usage:
    from carousel_suggester import suggest_carousel_assignments
    results = suggest_carousel_assignments(backend_url)
    # results: { carousel_id: {"title": str, "already_in": int, "to_add": [{"id", "name", "date"}]} }
"""

from datetime import datetime, timezone, timedelta

import requests

# ---------------------------------------------------------------------------
# Keyword maps: canonical keyword → list of surface forms to search for
# ---------------------------------------------------------------------------

MUSIC_KEYWORDS: list[tuple[str, list[str]]] = [
    ("טכנו",   ["טכנו", "techno"]),
    ("האוס",   ["האוס", "house"]),
    ("טראנס",  ["טראנס", "trance"]),
    ("היפ הופ", ["היפ הופ", "hip hop", "hiphop"]),
    ("ר&ב",    ["ר&ב", "rnb", "r&b", "r'n'b"]),
    ("מזרחי",  ["מזרחי", "mizrahi", "oriental"]),
    ("קומרשל", ["קומרשל", "commercial", "פופ", "pop"]),
    ("דראמ",   ["דראמ", "drum", "d&b", "dnb"]),
    ("רגאיי",  ["רגאיי", "reggae", "reggaeton"]),
    ("אלקטרו", ["אלקטרו", "electro", "electronic"]),
    ("פסיקו",  ["פסיקו", "psico", "psy", "פסיקדלי"]),
    ("דאבסטפ", ["דאבסטפ", "dubstep"]),
    ("אינדי",  ["אינדי", "indie", "alternative"]),
    ("מטאל",   ["מטאל", "metal", "rock"]),
    ("ג'אז",   ["ג'אז", "jazz", "blues"]),
    ("קלאסי",  ["קלאסי", "classical", "classic"]),
]

CITY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("תל אביב",  ["תל אביב", "tel aviv", "telaviv", "ת\"א", "יפו", "jaffa"]),
    ("ירושלים",  ["ירושלים", "jerusalem"]),
    ("חיפה",     ["חיפה", "haifa"]),
    ("באר שבע",  ["באר שבע", "beer sheva", "beersheba"]),
    ("אילת",     ["אילת", "eilat"]),
    ("נתניה",    ["נתניה", "netanya"]),
    ("הרצליה",   ["הרצליה", "herzliya"]),
    ("פתח תקווה", ["פתח תקווה", "petah tikva"]),
    ("ראשון",    ["ראשון לציון", "rishon"]),
    ("רמת גן",   ["רמת גן", "ramat gan"]),
    ("אשדוד",    ["אשדוד", "ashdod"]),
    ("חולון",    ["חולון", "holon"]),
    ("כרמיאל",   ["כרמיאל", "karmiel"]),
    ("גליל",     ["גליל", "galilee"]),
    ("הנגב",     ["נגב", "negev"]),
]

EVENT_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("מועדון", ["מועדון", "club", "קלאב", "נייטקלאב", "nightclub"]),
    ("פסטיבל", ["פסטיבל", "festival", "פסט"]),
    ("פרטי",   ["פרטי", "private", "סגור"]),
    ("בחוץ",   ["חיצוני", "open air", "openair", "שדה", "גן", "outdoor", "פארק"]),
    ("בר",     [" בר ", "bar ", " pub", "ברים"]),
]

AGE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("18+", ["18+", "18 +"]),
    ("21+", ["21+", "21 +"]),
    ("20+", ["20+", "20 +"]),
    ("16+", ["16+", "16 +"]),
]

TEMPORAL_KEYWORDS: list[str] = ["חם עכשיו", "hot now", "סוף שבוע", "this weekend", "שבוע", "עכשיו"]

# Days cap for location-based carousels (city match only, no other temporal filter)
LOCATION_DAYS_CAP = 60

# Party name/description keyword carousels — carousel title keyword → party name terms
NAME_KEYWORDS: list[tuple[str, list[str]]] = [
    ("אלכוהול חופשי", ["אלכוהול חופשי", "open bar", "פתוח בר"]),
    ("מסיבות ענק",    ["ענק", "mega", "מגה", "גדולה", "massive"]),
]


def _contains(text: str, terms: list[str]) -> bool:
    t = text.lower()
    return any(kw.lower() in t for kw in terms)


def _upcoming_weekend_dates(now: datetime) -> set[str]:
    """Return date strings (YYYY-MM-DD) for the upcoming Fri / Sat / Sun."""
    days_until_fri = (4 - now.weekday()) % 7
    if days_until_fri == 0:
        days_until_fri = 7
    fri = now + timedelta(days=days_until_fri)
    return {(fri + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(3)}


def _build_matchers(title: str):
    """
    Return a list of matcher callables for a given carousel title.
    Each matcher: (party: dict) -> bool
    """
    matchers = []
    tl = title.lower()

    # Temporal matchers
    if _contains(tl, ["חם עכשיו", "hot now"]):
        def hot_now(p, _cutoff=(datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d"),
                    _today=datetime.now(timezone.utc).strftime("%Y-%m-%d")):
            d = str(p.get("date") or p.get("startsAt", ""))[:10]
            return _today <= d <= _cutoff
        matchers.append(hot_now)

    if _contains(tl, ["סוף שבוע", "this weekend"]):
        weekend = _upcoming_weekend_dates(datetime.now(timezone.utc))
        def this_weekend(p, _w=weekend):
            d = str(p.get("date") or p.get("startsAt", ""))[:10]
            return d in _w
        matchers.append(this_weekend)

    # Music type matchers — also check party name and tags so "מסיבת טכנו" still matches
    for canonical, forms in MUSIC_KEYWORDS:
        if _contains(tl, [canonical] + forms):
            def music_matcher(p, _forms=forms):
                mt = (p.get("musicType") or "").lower()
                name = (p.get("name") or "").lower()
                tags = " ".join(p.get("tags") or []).lower()
                text = f"{mt} {name} {tags}"
                return any(f.lower() in text for f in _forms)
            matchers.append(music_matcher)

    # City matchers — capped to LOCATION_DAYS_CAP days from today
    # Also check name and tags so "מסיבה בתל אביב" still matches even with empty location
    _city_cutoff = (datetime.now(timezone.utc) + timedelta(days=LOCATION_DAYS_CAP)).strftime("%Y-%m-%d")
    for canonical, forms in CITY_KEYWORDS:
        if _contains(tl, [canonical] + forms):
            def city_matcher(p, _forms=forms, _cap=_city_cutoff):
                d = str(p.get("date") or p.get("startsAt", ""))[:10]
                if d > _cap:
                    return False
                loc = (p.get("location") or "").lower()
                name = (p.get("name") or "").lower()
                tags = " ".join(p.get("tags") or []).lower()
                text = f"{loc} {name} {tags}"
                return any(f.lower() in text for f in _forms)
            matchers.append(city_matcher)

    # Event type matchers
    for canonical, forms in EVENT_TYPE_KEYWORDS:
        if _contains(tl, [canonical] + forms):
            def etype_matcher(p, _forms=forms):
                et = (p.get("eventType") or "").lower()
                name = (p.get("name") or "").lower()
                return any(f.lower() in et or f.lower() in name for f in _forms)
            matchers.append(etype_matcher)

    # Age matchers
    for canonical, forms in AGE_KEYWORDS:
        if _contains(tl, [canonical] + forms):
            def age_matcher(p, _forms=forms):
                age = (p.get("age") or "").lower()
                return any(f.lower() in age for f in _forms)
            matchers.append(age_matcher)

    # Party name/description matchers — also check tags
    for canonical, forms in NAME_KEYWORDS:
        if _contains(tl, [canonical] + forms):
            def name_matcher(p, _forms=forms):
                name = (p.get("name") or p.get("title") or "").lower()
                desc = (p.get("description") or "").lower()
                tags = " ".join(p.get("tags") or []).lower()
                text = f"{name} {desc} {tags}"
                return any(f.lower() in text for f in _forms)
            matchers.append(name_matcher)

    return matchers


def suggest_carousels_for_party(party: dict, carousels: list) -> list[str]:
    """
    Return a list of carousel IDs that match the given party dict.
    Uses the same matcher logic as suggest_carousel_assignments.
    Skips past parties and carousels with no recognized keywords.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = str(party.get("date") or party.get("startsAt", ""))[:10]
    if d and d < today_str:
        return []

    result = []
    for carousel in carousels:
        cid = str(carousel.get("id") or carousel.get("_id", ""))
        title = carousel.get("title") or ""
        if _contains(title, ["חם עכשיו", "hot now"]):
            continue  # hot-now is account1-only, managed by run_hot_now_update
        matchers = _build_matchers(title)
        if not matchers:
            continue
        if any(m(party) for m in matchers):
            result.append(cid)
    return result


def suggest_carousel_assignments(backend_url: str) -> dict:
    """
    Fetch parties and carousels from the backend, then return suggested additions.

    Returns:
        {
          carousel_id: {
            "title": str,
            "already_in": int,
            "to_add": [{"id": str, "name": str, "date": str}]
          }
        }
    """
    backend_url = backend_url.rstrip("/")

    resp = requests.get(f"{backend_url}/api/parties?upcoming=true", timeout=15)
    all_parties: list[dict] = resp.json() if resp.status_code == 200 else []

    resp = requests.get(f"{backend_url}/api/carousels", timeout=10)
    carousels: list[dict] = resp.json() if resp.status_code == 200 else []

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results = {}
    for carousel in carousels:
        cid = str(carousel.get("id") or carousel.get("_id", ""))
        title = carousel.get("title") or ""
        current_ids = {str(pid) for pid in (carousel.get("partyIds") or [])}

        if _contains(title, ["חם עכשיו", "hot now"]):
            # hot-now is account1-only, managed exclusively by run_hot_now_update
            results[cid] = {"title": title, "already_in": len(current_ids), "to_add": [], "hot_now": True}
            continue

        matchers = _build_matchers(title)
        if not matchers:
            # No recognized keywords — skip automatic assignment
            results[cid] = {"title": title, "already_in": len(current_ids), "to_add": [], "no_matchers": True}
            continue

        to_add = []
        for party in all_parties:
            pid = str(party.get("_id") or party.get("id", ""))
            if not pid or pid in current_ids:
                continue
            # Skip past parties
            d = str(party.get("date") or party.get("startsAt", ""))[:10]
            if d and d < today_str:
                continue
            # Party matches if ANY matcher fires
            if any(m(party) for m in matchers):
                to_add.append({
                    "id": pid,
                    "name": party.get("name") or party.get("title") or pid,
                    "date": d,
                    "musicType": party.get("musicType") or "",
                    "location": party.get("location") or "",
                })

        results[cid] = {
            "title": title,
            "already_in": len(current_ids),
            "to_add": to_add,
        }

    return results
