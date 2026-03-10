"""
Subsurface Selections newsletter fetcher.

Iterates the beehiiv archive, fetches each post, and extracts structured
track recommendations. Each track entry is identified by a <strong>Artist – Title</strong>
pattern within a paragraph that also contains external music links.
"""
import re

from src.fetchers.common import get_html, make_soup, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE_URL = "https://subsurfaceselections.beehiiv.com"

# Domains that indicate a purchase/download link (preferred over Spotify)
_DOWNLOAD_DOMAINS = ("beatport.com", "bandcamp.com", "soundcloud.com", "on.soundcloud.com", "juno.co.uk")

# Separators used between artist and title in track entries
_TITLE_SEP = re.compile(r"\s[–—\-]\s")

# Known non-track strong text to skip (case-insensitive substrings)
_SKIP_PHRASES = ("record of the week", "new release", "out now", "listen", "download")


def _classify_links(anchors) -> tuple[str, str]:
    """Return (download_link, spotify_link) from a list of <a> tags."""
    download_link = ""
    spotify_link = ""
    for a in anchors:
        href = a.get("href", "")
        if not href or href.startswith("#"):
            continue
        if "spotify.com" in href:
            if not spotify_link:
                spotify_link = href
        elif any(d in href for d in _DOWNLOAD_DOMAINS):
            if not download_link:
                download_link = href
    return download_link, spotify_link


def _parse_post(post_url: str) -> list[SourceItem]:
    items = []
    try:
        html = get_html(post_url)
    except Exception as e:
        logger.warning(f"[subsurface] Failed to fetch post {post_url}: {e}")
        return items

    soup = make_soup(html)
    paragraphs = soup.find_all("p")

    for p in paragraphs:
        strong = p.find("strong")
        if not strong:
            continue

        strong_text = strong.get_text(strip=True)

        # Skip non-track headings/badges
        if any(phrase in strong_text.lower() for phrase in _SKIP_PHRASES):
            continue

        # Must contain artist–title separator
        if not _TITLE_SEP.search(strong_text):
            continue

        parts = _TITLE_SEP.split(strong_text, maxsplit=1)
        if len(parts) != 2:
            continue

        artist, title = parts[0].strip(), parts[1].strip()
        if not artist or not title:
            continue

        # Find external links in this paragraph
        anchors = p.find_all("a", href=True)
        download_link, spotify_link = _classify_links(anchors)

        primary_link = download_link or spotify_link
        if not primary_link:
            continue

        raw: dict = {"newsletter_post": post_url}
        if spotify_link:
            raw["spotify"] = spotify_link
        if download_link:
            raw["download"] = download_link

        items.append(
            SourceItem(
                source="subsurface_selections",
                artist=artist,
                title=title,
                link=primary_link,
                raw_metadata=raw,
            )
        )

    logger.info(f"[subsurface] {post_url} → {len(items)} tracks")
    return items


def _get_post_urls(archive_url: str) -> list[str]:
    try:
        html = get_html(archive_url)
    except Exception as e:
        logger.error(f"[subsurface] Failed to fetch archive: {e}")
        return []

    soup = make_soup(html)
    seen: set[str] = set()
    urls = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/p/"):
            full = _BASE_URL + href
            if full not in seen:
                seen.add(full)
                urls.append(full)
        elif href.startswith(_BASE_URL + "/p/"):
            if href not in seen:
                seen.add(href)
                urls.append(href)

    logger.info(f"[subsurface] Found {len(urls)} posts in archive")
    return urls


def fetch(settings) -> list[SourceItem]:
    cfg = settings.get_source_config("subsurface_selections")
    if not cfg.get("enabled", False):
        return []

    archive_url = cfg.get("archive_url", f"{_BASE_URL}/archive")
    post_urls = _get_post_urls(archive_url)

    all_items: list[SourceItem] = []
    for i, post_url in enumerate(post_urls):
        if i > 0:
            polite_sleep(1.5)
        all_items.extend(_parse_post(post_url))

    logger.info(f"[subsurface] Total tracks extracted: {len(all_items)}")
    return all_items
