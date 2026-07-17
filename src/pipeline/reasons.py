"""Deterministic reason composer for recommendation candidates.

Pure functions — no IO, no LLM. Every token comes from data present on the
Candidate or its matched ArtistProfile(s). Variant selection uses md5 so a
given track key always produces the same phrasing across runs and processes.
"""
import hashlib
from datetime import date, datetime, timezone
from typing import Optional

from src.models import ArtistProfile, Candidate
from src.pipeline.profile import _split_artists, resolve_profile


def _variant(key: str, n: int) -> int:
    return int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % n


def _days_phrase(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _plays_word(n: int) -> str:
    return "play" if n == 1 else "plays"


def compose_reason(
    c: Candidate,
    profiles_lower: dict[str, ArtistProfile],
    label_artists: Optional[dict[str, list[str]]] = None,
    today: Optional[date] = None,
    aliases: Optional[dict[str, str]] = None,
) -> str:
    """Return a one-sentence reason string for the candidate.

    today must be injected in tests (and the renderer passes it) to keep
    days_old deterministic. Defaults to UTC today in production.

    aliases: {alias_lower: canonical_lower} from Settings.artist_aliases();
    None/omitted preserves today's direct-match-only behaviour.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()

    signal_codes = {s.code for s in c.signals}

    # --- Fact extraction ---
    matched_profiles = [
        p for p in (
            resolve_profile(part, profiles_lower, aliases)
            for part in _split_artists(c.artist)
        )
        if p is not None
    ]

    play_count = max((p.play_count for p in matched_profiles), default=0)
    best_profile = None
    if matched_profiles:
        best_profile = max(matched_profiles, key=lambda p: p.play_count)

    prior: list[str] = []
    if best_profile:
        prior = [
            t for t in best_profile.track_titles
            if t.lower() != c.title.lower()
        ][:2]

    chart: Optional[int] = None
    raw_chart = c.raw_metadata.get("chart_position")
    if isinstance(raw_chart, int) and 1 <= raw_chart <= 100:
        chart = raw_chart

    dl_count: Optional[int] = None
    raw_dl = c.raw_metadata.get("download_count")
    if isinstance(raw_dl, int) and raw_dl > 0:
        dl_count = raw_dl

    sources = c.raw_metadata.get("seen_on_sources", [c.source])
    k = len(sources)
    srcs = ", ".join(s.title() for s in sources)

    days_old: Optional[int] = None
    if c.release_date:
        try:
            rel = date.fromisoformat(c.release_date[:10])
            d = (today - rel).days
            if 0 <= d <= 60:
                days_old = d
        except ValueError:
            pass

    label_names: list[str] = []
    if c.label and label_artists:
        label_names = label_artists.get(c.label.lower().strip(), [])

    a = best_profile.name if best_profile else ""
    source_disp = {"soundcloud": "SoundCloud"}.get(c.source, c.source.title())

    genre_disp = ""
    if c.genre_tags:
        raw_tags = c.genre_tags[:2]
        display_tags = ["UKG" if t == "ukg" else t for t in raw_tags]
        genre_disp = "/".join(display_tags)

    label_part = f" on {c.label}" if c.label else ""

    def _pick(variants: list[str]) -> str:
        eligible = [v for v in variants if _eligible(v)]
        if not eligible:
            return ""
        return eligible[_variant(c.key, len(eligible))]

    def _eligible(template: str) -> bool:
        """True if every placeholder referenced in template has a non-empty value."""
        checks = {
            "{a}": bool(a),
            "{n}": play_count > 0,
            "{p1}": bool(prior),
            "{p2}": len(prior) >= 2,
            "{pos}": chart is not None,
            "{k}": k >= 2,
            "{srcs}": bool(srcs) and k >= 2,
            "{label}": bool(c.label),
            "{names}": bool(label_names),
            "{g}": bool(genre_disp),
            "{d}": days_old is not None,
            "{label_part}": True,  # may be empty string — always eligible
            "{source_disp}": bool(source_disp),
            "{dl}": dl_count is not None,
        }
        for placeholder, ok in checks.items():
            if placeholder in template and not ok:
                return False
        return True

    def _fill(template: str) -> str:
        """Substitute all placeholders."""
        n_val = play_count
        p1 = prior[0] if prior else ""
        p2 = prior[1] if len(prior) >= 2 else ""
        d_phrase = _days_phrase(days_old) if days_old is not None else ""
        names_disp = ""
        if label_names:
            first3 = label_names[:3]
            names_disp = ", ".join(first3)
            if len(label_names) > 3:
                names_disp += ", …"

        result = template
        result = result.replace("{a}", a)
        result = result.replace("{n}", f"{n_val} {_plays_word(n_val)}")
        result = result.replace("{p1}", p1)
        result = result.replace("{p2}", p2)
        result = result.replace("{pos}", str(chart) if chart is not None else "")
        result = result.replace("{k}", str(k))
        result = result.replace("{srcs}", srcs)
        result = result.replace("{label}", c.label or "")
        result = result.replace("{names}", names_disp)
        result = result.replace("{g}", genre_disp)
        result = result.replace("{d}", d_phrase)
        result = result.replace("{label_part}", label_part)
        result = result.replace("{source_disp}", source_disp)
        result = result.replace("{dl}", str(dl_count) if dl_count is not None else "")
        return result

    # --- Template table (first matching row, top-to-bottom) ---

    if "known_artist" in signal_codes and chart is not None:
        row = _pick([
            "You play {a} ({p1}) — now #{pos} on the {source_disp} {g} chart.",
            "{a} again — #{pos} on {source_disp} {g}; you've played {p1}.",
            "You play {a} — now #{pos} on the {source_disp} {g} chart.",
            "You play {a} — charting at #{pos} on {source_disp}.",
        ])
        if row:
            return _fill(row)

    if "known_artist" in signal_codes and prior:
        row = _pick([
            "New {a} — {n} across your mix history ({p1}, {p2}).",
            "{a} follow-up to {p1} — {n} in your mix history.",
        ])
        if row:
            return _fill(row)

    if "known_artist" in signal_codes:
        row = _pick([
            "{a} has {n} in your mix history — new material from them.",
        ])
        if row:
            return _fill(row)

    if "label_match" in signal_codes and label_names:
        if len(label_names) == 1:
            row = _pick([
                "{label} — home of {names}, who you play.",
                "On {label}, the label behind {names} in your crates.",
            ])
        else:
            row = _pick([
                "{label} — {names} release here; you play them all.",
                "On {label}, the label behind {names} in your crates.",
            ])
        if row:
            return _fill(row)

    if "label_match" in signal_codes:
        return _fill("{label} — a label connected to artists you play.")

    if "scene_adjacent" in signal_codes and label_names:
        row = _pick([
            "Label-mate of {names} on {label}.",
            "{label} — same label as {names} in your crates.",
        ])
        if row:
            return _fill(row)

    if chart is not None:
        row = _pick([
            "#{pos} on the {source_disp} {g} chart this week.",
            "Charting at #{pos} on {source_disp} {g}.",
            "#{pos} on the {source_disp} chart this week.",
        ])
        if row:
            return _fill(row)

    if "cross_source" in signal_codes and k >= 2:
        row = _pick([
            "Picked up by {k} stores this week ({srcs}).",
            "Surfaced on {k} sources: {srcs}.",
        ])
        if row:
            return _fill(row)

    if "bandcamp_discovery" in signal_codes:
        if genre_disp:
            return _fill("Independent Bandcamp find — {g}, outside the chart feeds.")
        return "Independent Bandcamp find — outside the chart feeds."

    if "source_popularity" in signal_codes and dl_count is not None:
        row = _pick([
            "Free DL — grabbed {dl} times on {source_disp}.",
            "{dl} downloads on {source_disp} already.",
            "DJs are on this — {dl} downloads on {source_disp}.",
        ])
        if row:
            return _fill(row)

    if "genre_match" in signal_codes and days_old is not None:
        return _fill("Fresh {g}{label_part}, out {d}.")

    if "genre_match" in signal_codes:
        return _fill("Tagged {g} — inside your genre map.")

    if days_old is not None:
        return _fill("Out {d}{label_part}.")

    # Fallback — always eligible
    return _fill("New release{label_part} via {source_disp}.")
