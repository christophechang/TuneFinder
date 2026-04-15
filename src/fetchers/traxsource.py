"""
Traxsource source fetcher — genre top-100 chart scraping.

Each genre has a URL in the form /genre/{id}/{slug}/top. We scrape the top-tracks
listing for each configured genre. The request requires full browser headers
(Accept, Accept-Language) or the server returns 404.

Track row structure:
  div.trk-row[data-trid]
    .tnum-pos .tnum  — chart position number
    .title a         — track title + link (relative href)
    a.com-artists    — artist name(s), may be multiple
    .label a         — label name
    .r-date          — release date (YYYY-MM-DD)
"""
from src.fetchers.common import get_html, make_soup, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://www.traxsource.com"


def _parse_track_row(row) -> SourceItem | None:
    try:
        title_el = row.select_one(".title a")
        artist_els = row.select("a.com-artists")
        label_el = row.select_one(".label a")
        date_el = row.select_one(".r-date")

        title = title_el.get_text(strip=True) if title_el else ""
        artist = ", ".join(a.get_text(strip=True) for a in artist_els) if artist_els else ""
        label = label_el.get_text(strip=True) if label_el else None
        release_date = date_el.get_text(strip=True) if date_el else None

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
            release_date=release_date,
        )
    except Exception as e:
        logger.debug(f"[traxsource] Failed to parse row: {e}")
        return None


def fetch(settings, target_genre: str | None = None) -> list[SourceItem]:
    cfg = settings.get_source_config("traxsource")
    if not cfg.get("enabled", False):
        return []

    genre_url_pattern = cfg.get("genre_url_pattern", f"{_BASE}/genre/{{id}}/{{slug}}")
    genres: list[dict] = cfg.get("genres", [])
    if target_genre is not None:
        genres = [genre for genre in genres if genre.get("name") == target_genre]

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
        rows = soup.select("div.trk-row[data-trid]")

        if not rows:
            logger.warning(
                f"[traxsource] No track rows found for {slug} — HTML structure may have changed."
            )
            polite_sleep(1.5)
            continue

        genre_items = []
        for pos, row in enumerate(rows, start=1):
            parsed = _parse_track_row(row)
            if parsed:
                parsed.genre_tags = [genre.get("name", slug)]
                # Use explicit position from HTML if available, else fall back to enumerate
                tnum_el = row.select_one(".tnum-pos .tnum")
                chart_pos = int(tnum_el.get_text(strip=True)) if tnum_el else pos
                parsed.raw_metadata["chart_position"] = chart_pos
                genre_items.append(parsed)

        logger.info(f"[traxsource] {slug}: {len(genre_items)} tracks")
        all_items.extend(genre_items)
        polite_sleep(2.0)

    logger.info(f"[traxsource] Total: {len(all_items)} items across {len(genres)} genres")
    return all_items
