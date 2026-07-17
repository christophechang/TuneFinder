"""
Deterministic report renderer.

No LLM — reasons from src.pipeline.reasons, layout from this module.
_sanitize_report and Discord-safe link formatting are preserved verbatim.
"""
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from src.logger import get_logger
from src.models import ArtistProfile, Candidate
from src.pipeline.harmonic import candidate_bpm, candidate_camelot
from src.pipeline.profile import _split_artists, resolve_profile
from src.pipeline.reasons import compose_reason

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Canonical ordering
# ---------------------------------------------------------------------------

_SECTION_ORDER = ("top_picks", "label_watch", "artist_watch", "wildcards", "deep_cuts", "free_downloads")


def _group_label_watch(label_watch: list[Candidate]) -> tuple[dict[str, list[Candidate]], list[Candidate]]:
    """(by_label in first-occurrence order, no_label) — the exact render grouping."""
    by_label: dict[str, list[Candidate]] = {}
    no_label: list[Candidate] = []
    for c in label_watch:
        if c.label:
            if c.label not in by_label:
                by_label[c.label] = []
            by_label[c.label].append(c)
        else:
            no_label.append(c)
    return by_label, no_label


def report_order(sections: dict[str, list[Candidate]]) -> list[Candidate]:
    """Candidates in exact rendered-number order.

    Walks _SECTION_ORDER (absent keys skipped); 'label_watch' is expanded
    via _group_label_watch (grouped labels first, then no-label tracks).
    Raises ValueError on a section key not in _SECTION_ORDER. Works for
    weekly and mix-prep section dicts.
    """
    unknown = set(sections) - set(_SECTION_ORDER)
    if unknown:
        raise ValueError(f"Unknown section key(s): {', '.join(sorted(unknown))}")

    result: list[Candidate] = []
    seen: set[int] = set()

    def _add(c: Candidate) -> None:
        if id(c) not in seen:
            result.append(c)
            seen.add(id(c))

    for key in _SECTION_ORDER:
        if key not in sections:
            continue
        if key == "label_watch":
            by_label, no_label = _group_label_watch(sections[key])
            for label_candidates in by_label.values():
                for c in label_candidates:
                    _add(c)
            for c in no_label:
                _add(c)
        else:
            for c in sections[key]:
                _add(c)

    return result

# ---------------------------------------------------------------------------
# Discord output sanitiser
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^)<>]+)\)')
_BARE_URL_RE = re.compile(r'(?<![(<])(https?://\S+)(?![)>])')


def _sanitize_report(text: str) -> str:
    """Post-process report: suppress Discord embeds and remove duplicate URLs per line."""
    lines = []
    for line in text.split("\n"):
        line = _LINK_RE.sub(lambda m: f'[{m.group(1)}](<{m.group(2)}>)', line)
        line = _BARE_URL_RE.sub('', line).strip()
        seen: set[str] = set()

        def _dedup(m: re.Match) -> str:
            url = m.group(2)
            if url in seen:
                return ''
            seen.add(url)
            return m.group(0)

        line = re.sub(r'\[([^\]]*)\]\(<(https?://[^>]+)>\)', _dedup, line)
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stats helpers (kept verbatim from original)
# ---------------------------------------------------------------------------

def _format_weekly_stats(
    sections: dict[str, list[Candidate]],
    profiles: dict[str, ArtistProfile] | None,
    aliases: dict[str, str] | None = None,
) -> str:
    """Compact stats line rendered in the report header."""
    all_c = [c for sec in sections.values() for c in sec]
    if not all_c:
        return ""
    total = len(all_c)
    labels = {c.label for c in all_c if c.label}
    profiles_lower = {k.lower(): v for k, v in (profiles or {}).items()}
    known_artists = set()
    for c in all_c:
        for part in _split_artists(c.artist):
            profile = resolve_profile(part, profiles_lower, aliases)
            if profile:
                known_artists.add(profile.name.lower())
    genre_counts: dict[str, int] = {}
    for c in all_c:
        for g in c.genre_tags:
            genre_counts[g] = genre_counts.get(g, 0) + 1
    top_genres = [g for g, _ in sorted(genre_counts.items(), key=lambda kv: -kv[1])[:3]]
    return (
        f"This week: {total} tracks across {len(labels)} labels, "
        f"{len(known_artists)} known artists. "
        f"Top genres: {', '.join(top_genres) if top_genres else 'none tagged'}."
    )


def _format_mix_prep_stats(sections: dict[str, list[Candidate]]) -> str:
    """Compact stats line for mix-prep — totals and top genres only."""
    all_c = [c for sec in sections.values() for c in sec]
    if not all_c:
        return ""
    total = len(all_c)
    genre_counts: dict[str, int] = {}
    for c in all_c:
        for g in c.genre_tags:
            genre_counts[g] = genre_counts.get(g, 0) + 1
    top_genres = [g for g, _ in sorted(genre_counts.items(), key=lambda kv: -kv[1])[:3]]
    return f"This set: {total} tracks. Top genres: {', '.join(top_genres) if top_genres else 'none tagged'}."


def _format_fetcher_health(health: dict) -> str:
    """Format per-source fetch counts and errors for the report."""
    if not health:
        return ""
    lines = []
    for source, info in health.items():
        count = info.get("count", 0)
        error = info.get("error")
        if error:
            lines.append(f"❌ **{source}**: FAILED — {error}")
        elif count == 0:
            lines.append(f"⚠️ **{source}**: 0 tracks (possible schema/config issue)")
        else:
            lines.append(f"✅ **{source}**: {count} tracks")
    return "\n".join(lines)


def _report_link(settings, report_id: str) -> tuple[str | None, str]:
    """(url, label) for the footer link — the web app supersedes the audition
    page when TUNEFINDER_WEB_BASE_URL is set; audition stays the fallback."""
    web_base = getattr(settings, "web_base_url", "") or ""
    if web_base:
        return f"{web_base}/reports/{report_id}", "🎧 Open in TuneFinder"
    base_url = getattr(settings, "audition_base_url", "") or ""
    if base_url:
        return f"{base_url}/audition_{report_id}.html", "🎧 Audition Page"
    return None, "🎧 Audition Page"


def _build_footer(report_id: str, stats: dict, recommended_count: int | None = None, audition_url: str | None = None, audition_label: str = "🎧 Audition Page") -> str:
    """Build the Processing Summary and Fetcher Health footer."""
    lines = ["## ⚙️ Processing Summary"]
    lines.append(f"📥 Sources fetched: **{stats.get('sources_fetched', '?')}**")
    lines.append(f"🔀 After dedup: **{stats.get('after_dedup', stats.get('sources_fetched', '?'))}**")
    if "after_known" in stats:
        lines.append(f"🎵 After known-track filter: **{stats['after_known']}**")
    if "after_history" in stats:
        lines.append(f"📋 After history filter: **{stats['after_history']}**")
    if "after_genre" in stats:
        lines.append(f"🎚️ After genre filter: **{stats['after_genre']}**")
    if "pool_injected" in stats:
        lines.append(f"♻️ Pool injected: **{stats['pool_injected']}**")
    if "after_harmonic" in stats:
        lines.append(f"🎚️ After BPM/key filter: **{stats['after_harmonic']}**")
    if recommended_count is not None:
        lines.append(f"🎯 Tracks in report: **{recommended_count}**")
    lines.append(f"`Report ID: {report_id}`")
    if audition_url:
        lines.append(f"[{audition_label}](<{audition_url}>)")

    health = stats.get("fetcher_health", {})
    if health:
        lines.append("")
        lines.append("## 🔌 Fetcher Health")
        lines.append(_format_fetcher_health(health))

    return "\n".join(lines)


def _build_mix_prep_header(report_id: str, today: str, genre: str, filters_desc: str | None = None) -> str:
    display_genre = genre.upper() if genre == "ukg" else genre.replace("-", " ").title()
    lines = [
        f"🎛️ {display_genre} Mix Prep Report",
        f"Report ID: {report_id}",
        f"Date: {today}",
    ]
    if filters_desc:
        lines.append(filters_desc)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Track line formatter
# ---------------------------------------------------------------------------

# Brand names naive .title() gets wrong ("Soundcloud"). Other sources are unaffected.
_SOURCE_DISPLAY = {"soundcloud": "SoundCloud"}


def _track_line(n: int, c: Candidate, show_harmonic: bool = False) -> str:
    label_str = f" [{c.label}]" if c.label else ""
    source_str = f" [{_SOURCE_DISPLAY.get(c.source, c.source.title())}]"
    link_str = f" → [Listen](<{c.link}>)" if c.link else ""
    harmonic_str = ""
    if show_harmonic:
        # Mix-prep only (issue #8), and only when the caller's --bpm/--key
        # filters are active — the weekly report and filter-less mix-prep
        # runs never pass show_harmonic=True, so their track lines (and the
        # existing snapshot) are byte-for-byte unchanged.
        parts = []
        bpm = candidate_bpm(c)
        if bpm is not None:
            parts.append(f"{bpm:g} BPM")
        camelot = candidate_camelot(c)
        if camelot is not None:
            parts.append(camelot)
        if parts:
            harmonic_str = " · " + " · ".join(parts)
    return f"{n}. **{c.artist} — {c.title}**{label_str}{source_str}{link_str}{harmonic_str}"


# ---------------------------------------------------------------------------
# Label Watch artist-fact line
# ---------------------------------------------------------------------------

def _label_artist_line(label_key: str, label_artists: dict[str, list[str]]) -> str:
    """Return the italic artist-fact line for a label group, or "" if no names."""
    names = label_artists.get(label_key, [])
    if not names:
        return ""
    n = len(names)
    first3 = names[:3]
    names_disp = ", ".join(first3)
    if n > 3:
        names_disp += ", …"
    verb = "releases" if n == 1 else "release"
    return f"*{n} of your artists {verb} here: {names_disp}*"


# ---------------------------------------------------------------------------
# Main renderers
# ---------------------------------------------------------------------------

def generate_report(
    sections: dict[str, list[Candidate]],
    report_id: str,
    stats: dict,
    settings,
    profiles: dict[str, ArtistProfile] | None = None,
    label_artists: dict[str, list[str]] | None = None,
    today: Optional[date] = None,
    aliases: dict[str, str] | None = None,
) -> str:
    """Generate the full Discord-formatted weekly report deterministically."""
    if today is None:
        today = datetime.now(timezone.utc).date()

    profiles_lower = {k.lower(): v for k, v in (profiles or {}).items()}
    today_str = today.strftime("%-d %B %Y")
    stats_line = _format_weekly_stats(sections, profiles, aliases=aliases)

    lines = [f"**TuneFinder — {today_str} ({report_id})**"]
    if stats_line:
        lines.append(f"*{stats_line}*")
    lines.append("")

    track_counter = [0]

    def _render_track(c: Candidate) -> list[str]:
        track_counter[0] += 1
        reason = compose_reason(c, profiles_lower, label_artists=label_artists, today=today, aliases=aliases)
        return [_track_line(track_counter[0], c), f"> {reason}"]

    # Top Picks
    top_picks = sections.get("top_picks", [])
    if top_picks:
        lines.append("## 🔺 Top Picks")
        for c in top_picks:
            lines.extend(_render_track(c))
        lines.append("")

    # Label Watch — grouped by label
    label_watch = sections.get("label_watch", [])
    if label_watch:
        lines.append("## 🏷️ Label Watch")
        by_label, no_label = _group_label_watch(label_watch)
        for label_name, label_candidates in by_label.items():
            lines.append(f"**{label_name}**")
            artist_line = _label_artist_line(label_name.lower().strip(), label_artists or {})
            if artist_line:
                lines.append(artist_line)
            for c in label_candidates:
                lines.extend(_render_track(c))
        for c in no_label:
            lines.extend(_render_track(c))
        lines.append("")

    # Artist Watch
    artist_watch = sections.get("artist_watch", [])
    if artist_watch:
        lines.append("## 👁️ Artist Watch")
        for c in artist_watch:
            lines.extend(_render_track(c))
        lines.append("")

    # Wildcards
    wildcards = sections.get("wildcards", [])
    if wildcards:
        lines.append("## 🃏 Wildcards")
        for c in wildcards:
            lines.extend(_render_track(c))
        lines.append("")

    # Free Downloads — exclusive lane (pipeline.free_download_sources)
    free_downloads = sections.get("free_downloads", [])
    if free_downloads:
        lines.append("## 🆓 Free Downloads")
        for c in free_downloads:
            lines.extend(_render_track(c))
        lines.append("")

    recommended_count = sum(len(v) for v in sections.values())
    audition_url, audition_label = _report_link(settings, report_id)
    lines.append(_build_footer(report_id, stats, recommended_count, audition_url=audition_url, audition_label=audition_label))

    return _sanitize_report("\n".join(lines))


def generate_mix_prep_report(
    sections: dict[str, list[Candidate]],
    report_id: str,
    stats: dict,
    genre: str,
    settings,
    profiles: dict[str, ArtistProfile] | None = None,
    label_artists: dict[str, list[str]] | None = None,
    today: Optional[date] = None,
    aliases: dict[str, str] | None = None,
    filters_desc: str | None = None,
) -> str:
    """Generate a Discord-formatted mix-prep report focused on a single genre.

    filters_desc: human-readable summary of active --bpm/--key filters
    (issue #8, built by the CLI in tunefinder/__main__.cmd_mix_prep), e.g.
    "Filters: BPM 170–180 (±half/double) · key 8A±compat". None (default,
    the case for every existing caller/snapshot) omits the header line
    entirely and keeps track lines free of BPM/key suffixes — no filters
    active means byte-for-byte the same report as before this feature.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()

    profiles_lower = {k.lower(): v for k, v in (profiles or {}).items()}
    today_str = today.strftime("%-d %B %Y")
    stats_line = _format_mix_prep_stats(sections)
    header = _build_mix_prep_header(report_id, today_str, genre, filters_desc=filters_desc)
    show_harmonic = filters_desc is not None

    lines = [header, ""]
    if stats_line:
        lines.append(f"*{stats_line}*")
        lines.append("")

    track_counter = [0]

    def _render_track(c: Candidate) -> list[str]:
        track_counter[0] += 1
        reason = compose_reason(c, profiles_lower, label_artists=label_artists, today=today, aliases=aliases)
        return [_track_line(track_counter[0], c, show_harmonic=show_harmonic), f"> {reason}"]

    top_picks = sections.get("top_picks", [])
    if top_picks:
        lines.append(f"## 🔺 Top Picks ({genre})")
        for c in top_picks:
            lines.extend(_render_track(c))
        lines.append("")

    deep_cuts = sections.get("deep_cuts", [])
    if deep_cuts:
        lines.append("## 🎧 Deep Cuts")
        for c in deep_cuts:
            lines.extend(_render_track(c))
        lines.append("")

    free_downloads = sections.get("free_downloads", [])
    if free_downloads:
        lines.append("## 🆓 Free Downloads")
        for c in free_downloads:
            lines.extend(_render_track(c))
        lines.append("")

    audition_url, audition_label = _report_link(settings, report_id)
    lines.append(_build_footer(report_id, stats, audition_url=audition_url, audition_label=audition_label))

    return _sanitize_report("\n".join(lines))
