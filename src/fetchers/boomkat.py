"""
Boomkat source fetcher — website scraping.

Boomkat blocks plain requests with 403 if the User-Agent is not browser-like.
common.get_html() sends realistic headers. If this source still returns 403,
try adding a session cookie or rotating the User-Agent.

Boomkat release cards have a consistent structure:
  .product-title or h3.title — release name
  .product-artist or .artist — artist name
  .product-label or .label — label name
  a[href] on the card — release link
"""
from bs4 import Tag

from src.fetchers.common import get_html, make_soup, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://boomkat.com"


def _parse_release_card(card: Tag) -> SourceItem | None:
    try:
        # Title — try multiple selector patterns
        title_el = (
            card.select_one(".product-name a")
            or card.select_one("h3.title a")
            or card.select_one(".title a")
            or card.select_one("h3 a")
        )
        title = title_el.get_text(strip=True) if title_el else ""

        # Artist
        artist_el = (
            card.select_one(".product-artist")
            or card.select_one(".artist")
            or card.select_one("[class*='artist']")
        )
        artist = artist_el.get_text(strip=True) if artist_el else ""

        # Label
        label_el = (
            card.select_one(".product-label")
            or card.select_one(".label")
            or card.select_one("[class*='label']")
        )
        label = label_el.get_text(strip=True) if label_el else None

        # Link
        link_el = title_el or card.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        link = href if href.startswith("http") else f"{_BASE}{href}"

        if not title or not artist:
            return None

        return SourceItem(
            source="boomkat",
            artist=artist,
            title=title,
            link=link,
            label=label,
            release_name=title,
        )
    except Exception as e:
        logger.debug(f"[boomkat] Failed to parse card: {e}")
        return None


def fetch(settings) -> list[SourceItem]:
    cfg = settings.get_source_config("boomkat")
    if not cfg.get("enabled", False):
        return []

    url = cfg.get("new_releases_url", "https://boomkat.com/new-releases")
    logger.info(f"[boomkat] Fetching: {url}")

    try:
        html = get_html(url)
    except Exception as e:
        logger.warning(f"[boomkat] Failed to fetch {url}: {e}")
        return []

    soup = make_soup(html)

    # Try multiple candidate selectors for release cards
    cards = (
        soup.select(".product")
        or soup.select(".release-item")
        or soup.select("[class*='product-']")
        or soup.select("li.product")
    )

    if not cards:
        logger.warning(
            "[boomkat] No release cards found — HTML structure may have changed. "
            "Inspect the page and update the selector in boomkat.py."
        )
        return []

    results: list[SourceItem] = []
    for card in cards:
        parsed = _parse_release_card(card)
        if parsed:
            results.append(parsed)

    polite_sleep(1.5)
    logger.info(f"[boomkat] {len(results)} items")
    return results
