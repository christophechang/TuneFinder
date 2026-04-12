"""
Juno Download source fetcher — weekly bestsellers track chart scraper.

Scrapes the bestsellers *tracks* chart (singles/EPs only) for each configured
genre. The chart window (this-week / 4-weeks / 8-weeks / 6-months) is driven by
pipeline.release_date_window_days in settings.yaml and controls both the URL
slug and the release_date lower bound stamped on every returned SourceItem.

Key signals extracted:
  - chart_position: rank on the weekly track chart
  - bpm: tempo extracted from the listing
"""
import re
from datetime import date, timedelta

from src.fetchers.common import get_html, make_soup, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_BASE = "https://www.junodownload.com"
_CHART_URL = (
    "{base}/{slug}/charts/bestsellers/{window}/tracks/"
    "?items_per_page=100&music_product_type=single&limit_chart_period=1"
)

# Maps pipeline.release_date_window_days → Juno chart window URL slug.
# These are the only windows Juno supports.
_DAYS_TO_WINDOW: dict[int, str] = {
    7: "this-week",
    28: "four-weeks",
    56: "eight-weeks",
    180: "six-months",
}
_DEFAULT_WINDOW_DAYS = 7

# "4:08  /  174 BPM"
_BPM_RE = re.compile(r"(\d+)\s*BPM", re.IGNORECASE)


def _parse_track(card, genre: str, release_date: str | None = None) -> SourceItem | None:
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

    # Track title + link
    title_tag = card.find("a", class_="juno-title")
    if not title_tag:
        return None
    title = title_tag.get_text(strip=True)
    href = title_tag.get("href", "")
    link = (_BASE + href) if href.startswith("/") else href

    # Label (appears in two places for responsive layout — first is fine)
    label_tag = card.find("a", class_="juno-label")
    label = label_tag.get_text(strip=True) if label_tag else None

    # BPM + duration — "4:08  /  174 BPM"
    bpm = None
    tempo_div = card.find(class_="lit-date-length-tempo")
    if not tempo_div:
        # Mobile fallback inside lit-actions
        tempo_div = card.find("div", class_=lambda c: c and "d-sm-none" in c and "text-light" in c)
    if tempo_div:
        m = _BPM_RE.search(tempo_div.get_text())
        if m:
            bpm = int(m.group(1))

    return SourceItem(
        source="juno",
        artist=artist,
        title=title,
        link=link,
        label=label,
        release_date=release_date,
        genre_tags=[genre],
        raw_metadata={
            "juno_genre": genre,
            "chart_position": chart_position,
            "bpm": bpm,
        },
    )


def fetch(settings) -> list[SourceItem]:
    cfg = settings.get_source_config("juno")
    if not cfg.get("enabled", False):
        return []

    window_days: int = settings.pipeline_release_date_window_days or _DEFAULT_WINDOW_DAYS
    window_slug = _DAYS_TO_WINDOW.get(window_days)
    if window_slug is None:
        logger.warning(
            f"[juno] release_date_window_days={window_days} has no matching Juno window slug. "
            f"Valid values: {list(_DAYS_TO_WINDOW)}. Falling back to 'this-week'."
        )
        window_slug = "this-week"
        window_days = _DEFAULT_WINDOW_DAYS
    release_date = (date.today() - timedelta(days=window_days)).isoformat()
    logger.info(f"[juno] Using chart window '{window_slug}' → release_date lower bound {release_date}")

    genre_map: dict[str, str] = cfg.get("genre_map", {})
    all_items: list[SourceItem] = []

    for internal_genre, juno_slug in genre_map.items():
        url = _CHART_URL.format(base=_BASE, slug=juno_slug, window=window_slug)
        logger.info(f"[juno] Fetching track chart for {internal_genre}: {url}")

        try:
            html = get_html(url)
        except Exception as e:
            logger.warning(f"[juno] Failed to fetch {url}: {e}")
            polite_sleep(2.0)
            continue

        soup = make_soup(html)
        cards = soup.find_all("div", class_="jd-listing-item-track")
        logger.info(f"[juno] {juno_slug}: {len(cards)} tracks")

        for card in cards:
            item = _parse_track(card, internal_genre, release_date=release_date)
            if item:
                all_items.append(item)

        polite_sleep(2.0)

    logger.info(f"[juno] Total: {len(all_items)} tracks across {len(genre_map)} genres")
    return all_items
