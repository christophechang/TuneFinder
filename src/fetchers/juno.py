"""
Juno Download source fetcher — weekly bestsellers chart scraper.

Scrapes the this-week bestsellers chart (singles/EPs only) for each configured
genre. Each SourceItem represents a release; individual tracks are stored in
raw_metadata["tracks"] for use by the ranker.

Key signals extracted:
  - chart_position: rank on the weekly chart
  - has_review: editorial review present (quality indicator)
  - tracks[].is_hot_track: biggest-selling track on the release
  - tracks[].bpm: BPM where available
"""
import re
from datetime import datetime

from src.fetchers.common import get_html, make_soup, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://www.junodownload.com"
_CHART_URL = (
    "{base}/{slug}/charts/bestsellers/this-week/releases/"
    "?items_per_page=100&music_product_type=single"
)

# Track row text format: 'Artist Name - "Track Title" - (mm:ss) 123 BPM'
_TRACK_RE = re.compile(r'^(.+?)\s+-\s+"(.+?)"\s+-\s+\([\d:]+\)(?:\s+(\d+)\s*BPM)?', re.DOTALL)

_DATE_FMTS = ["%d %b %y", "%d %b %Y"]


def _parse_date(raw: str) -> str:
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw.strip()


def _parse_release(card, genre: str) -> SourceItem | None:
    # Chart position
    pos_tag = card.find(class_="listing-position")
    chart_position = int(pos_tag.get_text(strip=True)) if pos_tag else None

    # Artist(s)
    artist_div = card.find(class_="juno-artist")
    if not artist_div:
        return None
    artists = [a.get_text(strip=True) for a in artist_div.find_all("a") if a.get_text(strip=True)]
    artist = " / ".join(artists) if artists else artist_div.get_text(strip=True)
    if not artist:
        return None

    # Release title + link
    title_tag = card.find("a", class_="juno-title")
    if not title_tag:
        return None
    release_title = title_tag.get_text(strip=True)
    href = title_tag.get("href", "")
    link = (_BASE + href) if href.startswith("/") else href

    # Label
    label_tag = card.find("a", class_="juno-label")
    label = label_tag.get_text(strip=True) if label_tag else None

    # Catalog# and release date — "text-sm text-muted mt-3" info block
    catalog_num = None
    release_date = None
    info_div = card.find("div", class_=lambda c: c and "text-muted" in c and "mt-3" in c)
    if info_div:
        parts = [t.strip() for t in info_div.stripped_strings]
        if parts:
            catalog_num = parts[0]
        if len(parts) >= 2:
            release_date = _parse_date(parts[1])

    # Editorial review (quality signal — non-empty means it was hand-picked)
    review = None
    review_span = card.find("span", class_="text-primary")
    if review_span and "Review" in review_span.get_text():
        parent = review_span.parent
        if parent:
            review = parent.get_text(" ", strip=True).removeprefix("Review:").strip()

    # Individual tracks
    tracks = []
    tracklist = card.find(class_="jd-listing-tracklist")
    if tracklist:
        for text_div in tracklist.find_all("div", class_=["col", "pl-2"]):
            is_hot = bool(text_div.find("img", class_="icon-hot-track"))
            raw_text = text_div.get_text(" ", strip=True)
            m = _TRACK_RE.match(raw_text)
            if m:
                tracks.append({
                    "artist": m.group(1).strip(),
                    "title": m.group(2).strip(),
                    "bpm": int(m.group(3)) if m.group(3) else None,
                    "is_hot_track": is_hot,
                })

    return SourceItem(
        source="juno",
        artist=artist,
        title=release_title,
        link=link,
        label=label,
        release_date=release_date,
        release_name=release_title,
        genre_tags=[genre],
        raw_metadata={
            "juno_genre": genre,
            "chart_position": chart_position,
            "catalog_num": catalog_num,
            "has_review": bool(review),
            "review": review,
            "tracks": tracks,
        },
    )


def fetch(settings) -> list[SourceItem]:
    cfg = settings.get_source_config("juno")
    if not cfg.get("enabled", False):
        return []

    genre_map: dict[str, str] = cfg.get("genre_map", {})
    all_items: list[SourceItem] = []

    for internal_genre, juno_slug in genre_map.items():
        url = _CHART_URL.format(base=_BASE, slug=juno_slug)
        logger.info(f"[juno] Fetching chart for {internal_genre}: {url}")

        try:
            html = get_html(url)
        except Exception as e:
            logger.warning(f"[juno] Failed to fetch {url}: {e}")
            polite_sleep(2.0)
            continue

        soup = make_soup(html)
        cards = soup.find_all("div", class_="jd-listing-item")
        logger.info(f"[juno] {juno_slug}: {len(cards)} releases")

        for card in cards:
            item = _parse_release(card, internal_genre)
            if item:
                all_items.append(item)

        polite_sleep(2.0)

    logger.info(f"[juno] Total: {len(all_items)} items across {len(genre_map)} genres")
    return all_items
