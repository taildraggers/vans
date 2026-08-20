"""Scraper for Van's RV aircraft listings on barnstormers.com.

Barnstormers' single-manufacturer category pages (the same pattern seen in
the companion Aviat, CubCrafters, de Havilland, and Maule repos) can mix in
off-brand or off-topic listings with no distinguishing HTML markup from the
genuine ones - even when, as here, one of the categories is already scoped
to "Taildragger--Vans-RV". So results are filtered by title against a
small allowlist of Van's RV product names before being published.

Two Barnstormers categories are scraped: the taildragger-scoped one above,
and the general "Vans-RV" category (unlike the first, NOT pre-scoped to
taildraggers - it also carries tricycle-gear RVs). Unlike RANS/Luscombe/
Kitfox, Van's own model-naming convention makes gear type explicit and
unambiguous: a trailing "A" suffix (RV-7A, RV-9A, RV-14A, etc.) always
means tricycle gear, and RV-10/RV-12/RV-12iS have no taildragger version
at all. So those are excluded categorically in `_extract_model`, on top of
the same text-based tricycle/nosewheel safety net used in the companion
RANS, Luscombe, Just Aircraft, Kitfox, and Bellanca repos.

On top of that brand allowlist, only whole-aircraft-for-sale listings are
kept: each ad's title must match a recognized RV model code, and titles
that look like parts/accessories/services/raffles are dropped. Surviving
titles are rewritten to a canonical "YEAR Van's MODEL" form when the ad
states a model year, or just "Van's MODEL" when it doesn't, so every
listing follows the same format.

Pagination: Barnstormers' category pager here renders as page-number
buttons with no "Next" text or rel="next" attribute, so link-following
can't discover subsequent pages - `scrape()` builds each page's URL
directly from the known `?seocategory=<url-encoded-path>&page=<n>` pattern
instead.
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .common import (
    Listing,
    extract_date,
    extract_location,
    extract_price,
    fetch,
    format_aircraft_title,
)

SITE_NAME = "Barnstormers.com"
BASE = "https://www.barnstormers.com"
MAKE = "Van's"

# Category pages for Van's RV listings on Barnstormers: the
# taildragger-scoped one, plus the general one (which also carries
# tricycle-gear RVs - see module docstring).
CATEGORY_URLS = [
    f"{BASE}/category-22558-Taildragger--Vans-RV.html",
    f"{BASE}/category-23352-Vans-RV.html",
]

MAX_PAGES = 10
LISTING_LINK_RE = re.compile(r"^/classified-(\d+)-(.+)\.html$")
GENERIC_SITE_TITLE_SNIPPET = "barnstormers.com find aircraft"


def _compact(text: str) -> str:
    return re.sub(r"[\s-]", "", text.lower())


# RV model codes are "RV" + a 1-2 digit number, optionally followed by a
# trailing letter suffix (e.g. "RV-7A", "RV-12iS"). The prefix and number
# may be separated by a space, a hyphen, or nothing, since
# _title_from_url() turns the source URL's hyphens into spaces. A bare "rv"
# is deliberately NOT enough on its own (unlike "maule" for the Maule repo)
# since "rv" is too short/common a substring to trust without the digit -
# see _matches_target_models() below.
_MODEL_CODE_RE = re.compile(r"\brv[\s-]?(\d{1,2})([a-z]{0,2})\b", re.IGNORECASE)


def _matches_target_models(title: str) -> bool:
    compact = _compact(title)
    if "vans" in compact:
        return True
    return _MODEL_CODE_RE.search(title) is not None


# RV-10 and RV-12/RV-12iS have no taildragger version at all - they were
# designed exclusively with tricycle gear.
_ALWAYS_TRICYCLE_NUMBERS = {"10", "12"}

# Some sellers write the "A" gear suffix with its own separator (e.g.
# "RV7-A", "RV-6 A") rather than directly attached ("RV7A"). Since every
# listing's title is effectively URL-slug-derived in practice (see module
# docstring - Barnstormers' real <h1>/<title> is generic boilerplate on
# every page), _title_from_url() turns that separating hyphen into a
# space, making it indistinguishable from a genuinely space-separated "A" -
# so _MODEL_CODE_RE's attached-only suffix capture misses it entirely and
# a tricycle RV falls through as if it were the base taildragger model.
# Caught on real production data: "2026-Vans-RV7-A-45hrs-TT" and
# "Vans-RV6-A" were both being published as plain "RV-7"/"RV-6". This
# separately checks for a standalone "A" token following an RV number,
# requiring a real separator (not just \b) before it so it can't also
# start matching into an unrelated following word like "Airframe".
_DETACHED_A_SUFFIX_RE = re.compile(r"\brv[\s-]?\d{1,2}[\s-]+a\b", re.IGNORECASE)


def _extract_model(title: str) -> tuple[str, str] | None:
    match = _MODEL_CODE_RE.search(title)
    if not match:
        return None
    number, suffix = match.groups()
    if number in _ALWAYS_TRICYCLE_NUMBERS:
        return None
    if "a" in suffix.lower():
        return None
    if _DETACHED_A_SUFFIX_RE.search(title):
        return None
    model = f"RV-{number}{suffix.upper()}"
    return MAKE, model


# Ads whose title or body text explicitly calls out tricycle/nosewheel gear
# are dropped, regardless of which model they are - see module docstring.
_NON_TAILWHEEL_KEYWORDS = (
    "tricycle gear",
    "tricycle landing gear",
    "trike gear",
    "tri-gear",
    "tri gear",
    "nosewheel",
    "nose wheel",
    "nose-wheel",
)


def _is_non_tailwheel(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in _NON_TAILWHEEL_KEYWORDS)


def _title_from_url(url: str) -> str:
    """Listing pages share a generic <title>/<h1>, but the URL slug is the ad's own title."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    match = LISTING_LINK_RE.match("/" + slug)
    if not match:
        return unquote(slug)
    return unquote(match.group(2)).replace("-", " ").strip()


def _find_listing_links(html: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0]
        if LISTING_LINK_RE.match(href):
            links.add(urljoin(BASE, href))
    return links


def _page_url(category_url: str, page: int) -> str:
    """Build a category page's URL directly - see module docstring."""
    if page <= 1:
        return category_url
    path = urlparse(category_url).path
    return f"{category_url}?seocategory={quote(path, safe='')}&page={page}"


def _debug_dump_hrefs(html: str, limit: int = 25) -> None:
    soup = BeautifulSoup(html, "lxml")
    hrefs = [a["href"] for a in soup.find_all("a", href=True)]
    interesting = [h for h in hrefs if "classified" in h.lower() or "rv" in h.lower()]
    sample = interesting[:limit] or hrefs[:limit]
    print(f"  [debug] {len(hrefs)} total <a href> on page; sample: {sample}")


def _parse_detail_page(url: str, html: str) -> Listing | None:
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    if title:
        title = re.sub(r"\s*[\|\-]\s*Barnstormers.*$", "", title, flags=re.IGNORECASE).strip()
    if not title or GENERIC_SITE_TITLE_SNIPPET in title.lower():
        title = _title_from_url(url)
    if not title:
        return None

    if not _matches_target_models(title):
        return None

    text = soup.get_text(" ", strip=True)

    if _is_non_tailwheel(title) or _is_non_tailwheel(text):
        return None

    formatted_title = format_aircraft_title(title, text, _extract_model)
    if not formatted_title:
        return None
    title = formatted_title

    price = extract_price(text)
    location = extract_location(text)
    date_posted = extract_date(text)

    return Listing(
        title=title,
        price=price,
        location=location,
        date_posted=date_posted,
        site=SITE_NAME,
        url=url,
    )


def scrape() -> list[Listing]:
    print(f"[{SITE_NAME}] starting scrape")
    all_links: set[str] = set()

    for category_url in CATEGORY_URLS:
        seen_this_category: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            url = _page_url(category_url, page)
            html = fetch(url)
            if not html:
                break
            links = _find_listing_links(html)
            new_links = links - seen_this_category
            print(f"  [{category_url}] page {page}: {len(links)} links ({len(new_links)} new)")
            if page == 1 and not links:
                _debug_dump_hrefs(html)
            seen_this_category |= links
            if not new_links:
                break
        all_links |= seen_this_category

    print(f"[{SITE_NAME}] {len(all_links)} unique listing URLs found")

    candidate_links = {url for url in all_links if _matches_target_models(_title_from_url(url))}
    print(f"[{SITE_NAME}] {len(candidate_links)} match Van's RV product names")

    listings: list[Listing] = []
    for url in sorted(candidate_links):
        html = fetch(url)
        if not html:
            continue
        listing = _parse_detail_page(url, html)
        if listing:
            listings.append(listing)

    print(f"[{SITE_NAME}] parsed {len(listings)} listings")
    return listings
