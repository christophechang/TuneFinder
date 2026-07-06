from __future__ import annotations
import re
from datetime import datetime
from typing import Optional

from src.fetchers.common import get_html, make_soup, polite_sleep
from src.logger import get_logger
from src.models import SourceItem

logger = get_logger(__name__)

_SOURCE = "mixupload"
_BASE = "https://mixupload.com"
_CHART_URL = _BASE + "/charts/track/{chart}?date-month={date_month}"
_GENRE_TRACKS_URL = _BASE + "/genres/{genre}/page1"

# Maps lowercase Mixupload genre slugs → canonical TF tags.
# Covers both hyphenated and camelcase slug variants found in track card hrefs.
_SLUG_TO_TAG: dict[str, str] = {
    # House
    "house": "house", "housemusic": "house",
    "deep-house": "house", "deephouse": "house",
    "tech-house": "house", "techhouse": "house",
    "progressive-house": "house", "progressivehouse": "house",
    "afrohouse": "house", "afro-house": "house",
    "french-house": "house", "frenchhouse": "house",
    "funky-house": "house", "funkyhouse": "house",
    "soulful-house": "house", "soulfulhouse": "house",
    "ambient-house": "house", "ambienthouse": "house",
    "bass-house": "house", "basshouse": "house", "future-house": "house", "futurehouse": "house",
    "tropical-house": "house", "tropicalhouse": "house",
    "jazz-house": "house", "jazzhouse": "house",
    # Techno
    "techno": "techno",
    "acid-techno": "techno", "acidtechno": "techno",
    "dub-techno": "techno", "dubtechno": "techno",
    "minimal-techno": "techno", "minimaltechno": "techno",
    "detroit-techno": "techno", "detroittechno": "techno",
    "industrial-techno": "techno", "industrialtechno": "techno",
    "ambient-techno": "techno", "ambienttechno": "techno",
    "hard-techno": "techno", "hardtechno": "techno",
    # DNB
    "dnb": "dnb", "drumandbass": "dnb", "drum-and-bass": "dnb",
    "liquid-funk": "dnb", "liquidfunk": "dnb",
    "neurofunk": "dnb", "darkstep": "dnb", "drumfunk": "dnb",
    "drumstep": "dnb", "jazzstep": "dnb", "jump-up": "dnb", "jumpup": "dnb",
    "hardstep": "dnb", "techstep": "dnb",
    "intelligent-drum-and-bass": "dnb", "intelligentdrumandbass": "dnb",
    # Breaks
    "breaks": "breaks", "breakbeat": "breaks",
    "acid-breaks": "breaks", "acidbreaks": "breaks",
    "big-beat": "breaks", "bigbeat": "breaks",
    "broken-beat": "breaks", "brokenbeat": "breaks",
    "nu-skool-breaks": "breaks", "nuskoolbreaks": "breaks",
    "progressive-breaks": "breaks", "progressivebreaks": "breaks",
    # UK Bass
    "ukbass": "uk-bass", "uk-bass": "uk-bass",
    # UKG
    "ukgarage": "ukg", "uk-garage": "ukg",
    "2stepgarage": "ukg", "2-step-garage": "ukg",
    "future-garage": "ukg", "futuregarage": "ukg",
    "speed-garage": "ukg", "speedgarage": "ukg",
    "bassline": "ukg",
    # Electronica
    "electronica": "electronica",
    "berlin-school": "electronica", "berlinschool": "electronica",
    "laptronica": "electronica",
    "progressive-electronic": "electronica", "progressiveelectronic": "electronica",
    # Downtempo
    "downtempo": "downtempo",
    "chill-out": "downtempo", "chillout": "downtempo",
    "trip-hop": "downtempo", "triphop": "downtempo",
    "acid-jazz": "downtempo", "acidjazz": "downtempo",
    # Hip-hop
    "hiphop": "hip-hop", "hip-hop": "hip-hop",
    "alternative-hip-hop": "hip-hop", "alternativehiphop": "hip-hop",
    "instrumental-hip-hop": "hip-hop", "instrumentalhiphop": "hip-hop",
    "lo-fi-hip-hop": "hip-hop", "lofihiphop": "hip-hop",
}


def fetch(settings, target_genre: str | None = None) -> list[SourceItem]:
    cfg = settings.get_source_config(_SOURCE)
    if not cfg.get("enabled", False):
        return []

    targets: list[dict] = cfg.get("targets", [])
    date_month: str = datetime.now().strftime("%m.%Y")

    active = [t for t in targets
              if target_genre is None or t["tf_tag"] == target_genre]
    if not active:
        return []

    items: list[SourceItem] = []
    for target in active:
        tf_tag = target["tf_tag"]
        if "chart" in target:
            url = _CHART_URL.format(chart=target["chart"], date_month=date_month)
            is_chart = True
        else:
            url = _GENRE_TRACKS_URL.format(genre=target["genre"])
            is_chart = False

        try:
            html = get_html(url)
            if is_chart:
                fetched = _parse_chart_tracks(html, tf_tag, date_month)
            else:
                fetched = _parse_chart_tracks(html, tf_tag)
            logger.info(f"[mixupload] {url}: {len(fetched)} tracks")
            items.extend(fetched)
        except Exception as e:
            logger.warning(f"[mixupload] {url} fetch failed: {e}")
        polite_sleep(2.0)

    return items


def _parse_chart_tracks(html: str, genre_tag: str, period: str | None = None) -> list[SourceItem]:
    """Parse the chart page layout which uses .holder-player containers."""
    soup = make_soup(html)
    results = []

    for card in soup.select(".holder-player"):
        title_link = card.select_one("h3.for-sharing a")
        if not title_link:
            continue

        # The <a> contains two <div>s: first is the track title, second is artist name.
        # .made a is the uploader (may be a label account) — unreliable for artist.
        divs = title_link.select("div")
        title = divs[0].get_text(strip=True) if divs else title_link.get_text(strip=True)
        artist = divs[1].get_text(strip=True) if len(divs) > 1 else title_link.get_text(strip=True)
        if not artist:
            continue

        link_path = title_link.get("href", "")
        link = _BASE + link_path if link_path.startswith("/") else link_path

        # Position: .position contains a .num span and an optional .ext span (delta)
        pos_el = card.select_one(".position .num")
        chart_pos = _parse_position(pos_el.get_text(strip=True)) if pos_el else None

        # Date: inside .group > dl > dd
        date_el = card.select_one(".group dl dd")
        date = _parse_date(date_el.get_text(strip=True)) if date_el else None

        # BPM: .btn-track-info without .key class, text like "BPM: 125"
        bpm_el = card.select_one(".btn-track-info:not(.key)")
        bpm = _parse_bpm(bpm_el.get_text(strip=True)) if bpm_el else None

        # KEY: .btn-track-info.key, text like "KEY: Cm"
        key_el = card.select_one(".btn-track-info.key")
        key = _parse_key(key_el.get_text(strip=True)) if key_el else None

        # Download count: link to /track/download/<id>
        dl_link = card.select_one(".stat-track a[href*='/track/download/']")
        dl = _parse_count(dl_link.get_text(strip=True)) if dl_link else None

        # Stream count: text node after the fa-headphones icon in .stat-track
        stream = _parse_headphone_count(card.select_one(".stat-track"))

        # Genre tags: merge chart tag with any genre links on the card
        tags: set[str] = {genre_tag}
        for a in card.select("a[href*='/genres/']"):
            slug = a["href"].split("/genres/")[-1].rstrip("/")
            mapped = _SLUG_TO_TAG.get(slug.lower())
            if mapped:
                tags.add(mapped)

        raw: dict = {"bpm": bpm}
        if chart_pos is not None:
            raw["chart_position"] = chart_pos
        if period is not None:
            raw["chart_period"] = "month"
        if key is not None:
            raw["key"] = key
        if dl is not None:
            raw["download_count"] = dl
        if stream is not None:
            raw["stream_count"] = stream  # Parsed but unused in scoring — measures free listens, not intent

        results.append(SourceItem(
            source=_SOURCE,
            artist=artist,
            title=title,
            link=link,
            label=None,  # not present in chart card layout
            release_date=date,
            release_name=None,
            genre_tags=sorted(tags),
            raw_metadata=raw,
        ))

    return results



def _parse_headphone_count(stat_el) -> Optional[int]:
    """Extract the play/stream count from the text node after the fa-headphones icon."""
    if not stat_el:
        return None
    headphones = stat_el.find("i", class_="fa-headphones")
    if not headphones:
        return None
    next_sib = headphones.next_sibling
    if next_sib:
        return _parse_count(str(next_sib).strip())
    return None


def _parse_date(raw: str) -> Optional[str]:
    try:
        return datetime.strptime(raw.strip(), "%d.%m.%y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_position(raw: str) -> Optional[int]:
    m = re.match(r"(\d+)", raw.strip())
    return int(m.group(1)) if m else None


def _parse_bpm(raw: str) -> Optional[int]:
    m = re.search(r"(\d+)", raw)
    return int(m.group(1)) if m else None


def _parse_key(raw: str) -> Optional[str]:
    m = re.search(r"KEY:\s*(.+)", raw.strip(), re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_count(raw: str) -> Optional[int]:
    raw = raw.strip().lower().replace(",", "")
    m = re.search(r"([\d.]+)(k?)", raw)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2) == "k":
        val *= 1000
    return int(val)
