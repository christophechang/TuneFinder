"""
Traxsource source fetcher — genre page scraping.

Each genre has a URL in the form /genre/{id}/{slug}. We scrape the top-tracks
listing for each configured genre. The request requires full browser headers
(Accept, Accept-Language) or the server returns 404.

Track row structure:
  div.top-item[data-trid]
    a.com-title   — track title + link
    a.com-artists — artist name
    a.com-label   — label name
"""
from src.fetchers.common import get_html, make_soup, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://www.traxsource.com"


def _parse_track_row(row) -> SourceItem | None:
    try:
        title_el = row.select_one("a.com-title")
        artist_el = row.select_one("a.com-artists")
        label_el = row.select_one("a.com-label")

        title = title_el.get_text(strip=True) if title_el else ""
        artist = artist_el.get_text(strip=True) if artist_el else ""
        label = label_el.get_text(strip=True) if label_el else None

        href = title_el.get("href", "") if title_el else ""
        link = href if href.startswith("http") else f"{_BASE}{href}"

        if not title or not artist:
            return None

        return SourceItem(
            source="traxsource",
            artist=artist,
            title=title,
            link=link,
            label=label,
            release_name=title,
        )
    except Exception as e:
        logger.debug(f"[traxsource] Failed to parse row: {e}")
        return None


def fetch(settings) -> list[SourceItem]:
    cfg = settings.get_source_config("traxsource")
    if not cfg.get("enabled", False):
        return []

    genre_url_pattern = cfg.get("genre_url_pattern", f"{_BASE}/genre/{{id}}/{{slug}}")
    genres: list[dict] = cfg.get("genres", [])

    all_items: list[SourceItem] = []

    for genre in genres:
        gid = genre.get("id", "")
        slug = genre.get("slug", "")
        url = genre_url_pattern.format(id=gid, slug=slug)
        logger.info(f"[traxsource] Fetching {genre.get('name', slug)}: {url}")

        try:
            html = get_html(url)
        except Exception as e:
            logger.warning(f"[traxsource] Failed to fetch {url}: {e}")
            polite_sleep(1.5)
            continue

        soup = make_soup(html)
        rows = soup.select("div.top-item[data-trid]")

        if not rows:
            logger.warning(
                f"[traxsource] No track rows found for {slug} — HTML structure may have changed."
            )
            polite_sleep(1.5)
            continue

        genre_items = []
        for row in rows:
            parsed = _parse_track_row(row)
            if parsed:
                parsed.genre_tags = [genre.get("name", slug)]
                genre_items.append(parsed)

        logger.info(f"[traxsource] {slug}: {len(genre_items)} tracks")
        all_items.extend(genre_items)
        polite_sleep(2.0)

    logger.info(f"[traxsource] Total: {len(all_items)} items across {len(genres)} genres")
    return all_items
