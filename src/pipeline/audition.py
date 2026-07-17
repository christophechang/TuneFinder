"""
Audition page generator.

Produces a fully self-contained HTML file with inline players and copy-buttons
for the mark command. No network, no filesystem in the render function.

Player precedence per track:
  1. Bandcamp EmbeddedPlayer iframe (bandcamp_album_id from raw_metadata)
  2. Beatport embed iframe (beatport_id from raw_metadata)
  3. SoundCloud widget iframe (source == "soundcloud", embeds from the permalink)
  4. Link-only row

Step 0 findings:
  - Volumo: no preview URL field found → rows are link-only (no audio element).
  - Bandcamp: embed id is in `item_id` field (captured as bandcamp_album_id).
    Embed URL: https://bandcamp.com/EmbeddedPlayer/album={id}/size=small/...
  - Beatport: https://embed.beatport.com/?id={id}&type=track works.
"""
import html
import os
import shlex
import urllib.parse
from datetime import date, datetime, timezone
from typing import Optional

from src.pipeline.reasons import compose_reason
from src.pipeline.report import _SECTION_ORDER, _group_label_watch, report_order
from src.pipeline.storage import atomic_write_text

_AUDITION_RETAIN = 26


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

_CSS = """\
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; font-size: 14px; margin: 0; padding: 16px; background: #111; color: #eee; }
h1 { font-size: 18px; margin: 0 0 4px; }
.meta { color: #888; font-size: 12px; margin-bottom: 16px; }
h2 { font-size: 15px; color: #aef; margin: 20px 0 8px; border-bottom: 1px solid #333; padding-bottom: 4px; }
.track { display: flex; gap: 12px; margin-bottom: 14px; padding: 10px; background: #1a1a1a; border-radius: 6px; }
.player { flex: 0 0 auto; width: 300px; }
.player iframe { width: 300px; border: none; background: #222; border-radius: 4px; }
.info { flex: 1; min-width: 0; }
.num { color: #888; font-size: 12px; }
.title { font-weight: bold; font-size: 15px; margin: 2px 0; }
.meta-row { color: #999; font-size: 12px; margin: 2px 0; }
.reason { color: #bbb; font-size: 12px; margin: 4px 0; font-style: italic; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.btn { padding: 4px 10px; font-size: 12px; border: none; border-radius: 4px; cursor: pointer; background: #333; color: #eee; }
.btn:hover { background: #555; }
footer { margin-top: 24px; color: #555; font-size: 11px; }
"""

_JS = """\
function copyCmd(button) { navigator.clipboard.writeText(button.dataset.cmd); }
"""

_SECTION_LABELS = {
    "top_picks": "Top Picks",
    "label_watch": "Label Watch",
    "artist_watch": "Artist Watch",
    "wildcards": "Wildcards",
    "deep_cuts": "Deep Cuts",
    "free_downloads": "Free Downloads",
}


def _prettify_section(key: str) -> str:
    return _SECTION_LABELS.get(key, key.replace("_", " ").title())


def _player_html(c) -> str:
    """Return player HTML or empty string for link-only."""
    album_id = c.raw_metadata.get("bandcamp_album_id")
    if album_id and isinstance(album_id, int):
        src = (
            f"https://bandcamp.com/EmbeddedPlayer/album={album_id}"
            "/size=small/tracklist=false/artwork=small/"
        )
        return (
            f'<iframe loading="lazy" src="{html.escape(src)}" '
            f'height="42" scrolling="no"></iframe>'
        )

    beatport_id = c.raw_metadata.get("beatport_id")
    if beatport_id and isinstance(beatport_id, int):
        src = f"https://embed.beatport.com/?id={beatport_id}&type=track"
        return (
            f'<iframe loading="lazy" src="{html.escape(src)}" '
            f'height="54" scrolling="no"></iframe>'
        )

    if c.source == "soundcloud" and c.link:
        src = (
            "https://w.soundcloud.com/player/?url="
            + urllib.parse.quote(c.link, safe="")
            + "&auto_play=false&visual=false&show_user=true"
        )
        return (
            f'<iframe loading="lazy" src="{html.escape(src)}" '
            f'height="120" scrolling="no"></iframe>'
        )

    return ""


def _copy_button(label: str, outcome: str, cmd: str) -> str:
    safe_cmd = html.escape(cmd, quote=True)
    return (
        f'<button class="btn" onclick="copyCmd(this)" '
        f'data-cmd="{safe_cmd}">{html.escape(label)}</button>'
    )


def _build_mark_buttons(n: int, artist: str, title: str, mark_by_number: bool) -> str:
    outcomes = [("Bought", "bought"), ("Liked", "liked"), ("Skip", "skip"), ("Own", "own"), ("Heard", "heard")]
    buttons = []
    for label, outcome in outcomes:
        if mark_by_number:
            cmd = f"tunefinder mark {n} {outcome}"
        else:
            selector = shlex.quote(f"{artist} - {title}")
            cmd = f"tunefinder mark {selector} {outcome}"
        buttons.append(_copy_button(label, outcome, cmd))
    return "\n".join(buttons)


def _track_row(
    n: int,
    c,
    profiles_lower: dict,
    label_artists: Optional[dict],
    mark_by_number: bool,
    today: Optional[date],
    aliases: Optional[dict] = None,
) -> str:
    artist_safe = html.escape(c.artist)
    title_safe = html.escape(c.title)
    label_safe = html.escape(c.label) if c.label else ""
    sources = c.raw_metadata.get("seen_on_sources", [c.source])
    source_safe = html.escape(", ".join(s.title() for s in sources))

    tags_safe = html.escape(", ".join(c.genre_tags)) if c.genre_tags else ""
    bpm = c.raw_metadata.get("bpm")
    key_val = c.raw_metadata.get("keysign") or c.raw_metadata.get("key")
    meta_parts = []
    if tags_safe:
        meta_parts.append(tags_safe)
    if bpm:
        meta_parts.append(f"{bpm} BPM")
    if key_val:
        meta_parts.append(html.escape(str(key_val)))

    reason = html.escape(compose_reason(c, profiles_lower, label_artists=label_artists, today=today, aliases=aliases))
    player = _player_html(c)
    link_safe = html.escape(c.link) if c.link else ""
    link_html = f'<a href="{link_safe}" target="_blank" style="color:#6af;font-size:12px;">Open in store ↗</a>' if link_safe else ""
    buttons = _build_mark_buttons(n, c.artist, c.title, mark_by_number)

    player_col = f'<div class="player">{player}</div>' if player else ""

    return f"""\
<div class="track">
{player_col}
<div class="info">
<div class="num">#{n}</div>
<div class="title">{artist_safe} — {title_safe}</div>
<div class="meta-row">{html.escape(label_safe) if label_safe else ""}{" · " if label_safe else ""}{source_safe}</div>
{"<div class='meta-row'>" + " · ".join(meta_parts) + "</div>" if meta_parts else ""}
<div class="reason">{reason}</div>
<div class="actions">{link_html}&nbsp;{buttons}</div>
</div>
</div>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_audition_page(
    sections: dict,
    report_id: str,
    settings,
    profiles: Optional[dict] = None,
    label_artists: Optional[dict] = None,
    mark_by_number: bool = True,
    today: Optional[date] = None,
    aliases: Optional[dict] = None,
) -> str:
    """Render a self-contained audition HTML page. Pure — no IO."""
    if today is None:
        today = datetime.now(timezone.utc).date()

    profiles_lower = {k.lower(): v for k, v in (profiles or {}).items()}
    ordered = report_order(sections)
    track_count = len(ordered)
    today_str = today.strftime("%-d %B %Y")

    lines = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'>",
        f"<title>Audition — {html.escape(report_id)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>Audition Queue — {html.escape(report_id)}</h1>",
        f"<div class='meta'>{html.escape(today_str)} · {track_count} tracks</div>",
    ]

    # Build a counter that matches report_order (same as mark numbering)
    counter = 0
    pos_map: dict[int, int] = {}  # id(c) → track number
    for c in ordered:
        counter += 1
        pos_map[id(c)] = counter

    # Walk sections in _SECTION_ORDER to emit headers
    for section_key in _SECTION_ORDER:
        if section_key not in sections or not sections[section_key]:
            continue

        lines.append(f"<h2>{html.escape(_prettify_section(section_key))}</h2>")

        if section_key == "label_watch":
            by_label, no_label = _group_label_watch(sections[section_key])
            group_candidates = [c for grp in by_label.values() for c in grp] + no_label
            for c in group_candidates:
                n = pos_map[id(c)]
                lines.append(_track_row(n, c, profiles_lower, label_artists, mark_by_number, today, aliases))
        else:
            for c in sections[section_key]:
                n = pos_map[id(c)]
                lines.append(_track_row(n, c, profiles_lower, label_artists, mark_by_number, today, aliases))

    lines.extend([
        f"<footer>generated by TuneFinder v-deterministic — reasons identical to the Discord report</footer>",
        f"<script>{_JS}</script>",
        "</body>",
        "</html>",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File I/O (Commit 4 adds this)
# ---------------------------------------------------------------------------

def write_audition_page(html_content: str, data_dir: str, report_id: str) -> str:
    """Write audition HTML to data_dir/reports/, prune beyond 26, return path."""
    reports_dir = os.path.join(data_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    path = os.path.join(reports_dir, f"audition_{report_id}.html")
    atomic_write_text(path, html_content)

    # Prune audition_*.html beyond most recent 26 by mtime
    candidates = sorted(
        [os.path.join(reports_dir, fn) for fn in os.listdir(reports_dir) if fn.startswith("audition_") and fn.endswith(".html")],
        key=os.path.getmtime,
    )
    for old in candidates[:-_AUDITION_RETAIN]:
        os.remove(old)

    return path
