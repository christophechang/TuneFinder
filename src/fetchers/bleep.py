"""
Bleep source fetcher — website scraping.

Bleep may block requests without browser-like headers (403).
common.get_html() sends realistic headers.

Bleep release cards typically have:
  a.product-name or .title — release name + link
  .artist or .artist-name — artist
  .label or .label-name — label (not always present on listing pages)
"""
from bs4 import Tag

from src.fetchers.common import get_html, make_soup, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://bleep.com"


def _parse_release_card(card: Tag) -> SourceItem | None:
    try:
        # Title
        title_el = (
            card.select_one(".product-name a")
            or card.select_one(".title a")
            or card.select_one("h3 a")
            or card.select_one("h2 a")
        )
        title = title_el.get_text(strip=True) if title_el else ""

        # Artist
        artist_el = (
            card.select_one(".artist-name a")
            or card.select_one(".artist a")
            or card.select_one("[class*='artist']")
        )
        artist = artist_el.get_text(strip=True) if artist_el else ""

        # Label
        label_el = (
            card.select_one(".label-name a")
            or card.select_one(".label a")
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
            source="bleep",
            artist=artist,
            title=title,
            link=link,
            label=label,
            release_name=title,
        )
    except Exception as e:
        logger.debug(f"[bleep] Failed to parse card: {e}")
        return None


def fetch(settings) -> list[SourceItem]:
    cfg = settings.get_source_config("bleep")
    if not cfg.get("enabled", False):
        return []

    url = cfg.get("new_releases_url", "https://bleep.com/release/new")
    logger.info(f"[bleep] Fetching: {url}")

    try:
        html = get_html(url)
    except Exception as e:
        logger.warning(f"[bleep] Failed to fetch {url}: {e}")
        return []

    soup = make_soup(html)

    cards = (
        soup.select(".product")
        or soup.select(".release")
        or soup.select("[class*='release-item']")
        or soup.select("li.item")
    )

    if not cards:
        logger.warning(
            "[bleep] No release cards found — HTML structure may have changed. "
            "Inspect the page and update the selector in bleep.py."
        )
        return []

    results: list[SourceItem] = []
    for card in cards:
        parsed = _parse_release_card(card)
        if parsed:
            results.append(parsed)

    polite_sleep(1.5)
    logger.info(f"[bleep] {len(results)} items")
    return results
