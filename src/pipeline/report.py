"""
Report generator — two-stage LLM pipeline.

Stage 1 (cheap, fast):
  Enriches the reason field for each shortlisted candidate using the Stage 1
  cascade chain. One batch call covering all sections.
  Falls back to signal-derived reasons if Stage 1 fails.

Stage 2:
  Writes the full Discord-formatted weekly report from the enriched candidates.
  One call per run.
"""
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

from src.llm import call_stage1, call_stage2
from src.logger import get_logger
from src.models import Candidate
from src.pipeline.label_cache import load_label_profiles, save_label_profiles


def _clean_llm_json(raw: str) -> str:
    """Strip markdown code fences and replace control characters that break json.loads."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    # Replace all control characters with a space.
    # LLMs occasionally embed raw newlines/tabs inside string values, which is invalid JSON.
    # Replacing with spaces is safe — JSON parsers treat spaces as whitespace between tokens.
    raw = re.sub(r"[\x00-\x1f\x7f]", " ", raw)
    return raw

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Discord output sanitiser
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^)<>]+)\)')
_BARE_URL_RE = re.compile(r'(?<![(<])(https?://\S+)(?![)>])')


def _sanitize_report(text: str) -> str:
    """Post-process report: suppress Discord embeds and remove duplicate URLs per line."""
    lines = []
    for line in text.split("\n"):
        # Convert [text](url) → [text](<url>) to suppress Discord embed previews
        line = _LINK_RE.sub(lambda m: f'[{m.group(1)}](<{m.group(2)}>)', line)
        # Remove any remaining bare URLs (not inside <> or markdown links) — these trigger embeds
        line = _BARE_URL_RE.sub('', line).strip()
        # Deduplicate: if the same URL appears twice in masked links on one line, keep first only
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


_DJ_CONTEXT = (
    "The DJ plays D&B, Breakbeat, UK Bass, UK Garage, House, Techno, and Electronica. "
    "Mix genre is a soft signal — tracks appear across scene boundaries. "
    "The audience is the DJ themselves reviewing their own weekly discovery feed."
)


# ---------------------------------------------------------------------------
# Stage 1 — reason enrichment
# ---------------------------------------------------------------------------

def _signal_summary(c: Candidate) -> list[str]:
    return [s.explanation for s in c.signals]


def _enrich_reasons(candidates: list[Candidate], settings) -> dict[str, str]:
    """
    Call Stage 1 LLM to generate a punchy one-sentence reason per candidate.
    Returns dict of {artist||title: reason}. Falls back to signal text on failure.
    """
    payload = [
        {
            "artist": c.artist,
            "title": c.title,
            "label": c.label or "",
            "signals": _signal_summary(c),
        }
        for c in candidates
    ]

    system = (
        "You write concise music discovery reasons for a DJ. "
        f"{_DJ_CONTEXT} "
        "For each track, write one punchy sentence (max 15 words) explaining "
        "why it fits this DJ's taste, based on the provided signals. "
        "Be specific and musical — name drop the context where relevant. "
        "Return a valid JSON array only, no preamble or explanation."
    )
    prompt = (
        f"Generate reasons for these tracks:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        'Return format: [{"artist": "...", "title": "...", "reason": "..."}]'
    )

    try:
        raw = _clean_llm_json(call_stage1(prompt, system, settings))
        enriched = json.loads(raw)
        return {
            f"{e['artist'].lower().strip()}||{e['title'].lower().strip()}": e.get("reason", "")
            for e in enriched
            if "artist" in e and "title" in e
        }
    except Exception as e:
        logger.warning(f"[report] Stage 1 reason enrichment failed: {e} — using signal fallback")
        return {}


# ---------------------------------------------------------------------------
# Label synopsis enrichment
# ---------------------------------------------------------------------------

def _enrich_label_synopses(labels: list[str], settings, data_dir: str) -> dict[str, str]:
    """
    Return a synopsis string for each label name.
    Hits the cache first; only calls Stage 1 LLM for labels not yet cached.
    Saves new entries back to cache.
    Returns dict keyed by original label name (preserves case for display).
    """
    cache = load_label_profiles(data_dir)
    result: dict[str, str] = {}
    missing: list[str] = []

    for label in labels:
        cached = cache.get(label.lower().strip())
        if cached:
            result[label] = cached
        else:
            missing.append(label)

    if missing:
        system = (
            "You write concise record label descriptions for a DJ's music discovery report. "
            "For each label write one sentence (max 20 words) covering: founding city/country, "
            "approximate founding year if known, and 2-3 key artists. Be factual and specific. "
            "Return a valid JSON array only, no preamble."
        )
        prompt = (
            f"Generate synopses for these labels:\n{json.dumps(missing, ensure_ascii=False)}\n\n"
            'Return format: [{"label": "...", "synopsis": "..."}]'
        )
        try:
            raw = _clean_llm_json(call_stage1(prompt, system, settings))
            enriched = json.loads(raw)
            for e in enriched:
                if "label" in e and "synopsis" in e:
                    result[e["label"]] = e["synopsis"]
                    cache[e["label"].lower().strip()] = e["synopsis"]
            save_label_profiles(cache, data_dir)
        except Exception as e:
            logger.warning(f"[report] Label synopsis enrichment failed: {e}")

    return result


def _format_label_watch_for_prompt(
    candidates: list[Candidate],
    reasons: dict[str, str],
    synopses: dict[str, str],
) -> str:
    """Format LABEL WATCH section grouped by label, with synopsis per label group."""
    if not candidates:
        return ""

    by_label: dict[str, list[Candidate]] = defaultdict(list)
    no_label: list[Candidate] = []
    for c in candidates:
        if c.label:
            by_label[c.label].append(c)
        else:
            no_label.append(c)

    lines = ["LABEL WATCH (active labels in your scene):"]
    for label_name, label_candidates in by_label.items():
        synopsis = synopses.get(label_name, "")
        lines.append(f"  [LABEL: {label_name}]")
        if synopsis:
            lines.append(f"  [SYNOPSIS: {synopsis}]")
        for c in label_candidates:
            key = f"{c.artist.lower().strip()}||{c.title.lower().strip()}"
            reason = reasons.get(key) or c.primary_reason or "Interesting new release."
            source_tag = f" [SOURCE:{c.source.title()}]" if c.source else ""
            link_str = f" {c.link}" if c.link else ""
            lines.append(f"    - {c.artist} — {c.title} [{label_name}]{source_tag} | {reason}{link_str}")

    # Tracks with no label fall through without a sub-header
    for c in no_label:
        key = f"{c.artist.lower().strip()}||{c.title.lower().strip()}"
        reason = reasons.get(key) or c.primary_reason or "Interesting new release."
        source_tag = f" [SOURCE:{c.source.title()}]" if c.source else ""
        link_str = f" {c.link}" if c.link else ""
        lines.append(f"  - {c.artist} — {c.title}{source_tag} | {reason}{link_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 2 — full report
# ---------------------------------------------------------------------------

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


def _build_footer(report_id: str, stats: dict, recommended_count: int | None = None) -> str:
    """Build the Processing Summary and Fetcher Health footer in plain Discord-friendly text."""
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
    if recommended_count is not None:
        lines.append(f"🎯 Tracks in report: **{recommended_count}**")
    lines.append(f"`Report ID: {report_id}`")

    health = stats.get("fetcher_health", {})
    if health:
        lines.append("")
        lines.append("## 🔌 Fetcher Health")
        lines.append(_format_fetcher_health(health))

    return "\n".join(lines)


def _format_section_for_prompt(label: str, candidates: list[Candidate], reasons: dict[str, str]) -> str:
    if not candidates:
        return ""
    lines = [f"{label}:"]
    for c in candidates:
        key = f"{c.artist.lower().strip()}||{c.title.lower().strip()}"
        reason = reasons.get(key) or c.primary_reason or "Interesting new release."
        label_str = f" [{c.label}]" if c.label else ""
        source_tag = f" [SOURCE:{c.source.title()}]" if c.source else ""
        link_str = f" {c.link}" if c.link else ""
        lines.append(f"  - {c.artist} — {c.title}{label_str}{source_tag} | {reason}{link_str}")
    return "\n".join(lines)


def generate_report(
    sections: dict[str, list[Candidate]],
    report_id: str,
    stats: dict,
    settings,
) -> str:
    """
    Generate the full Discord-formatted weekly report.
    Calls Stage 1 to enrich reasons and label synopses, then Stage 2 to write the report.
    Returns the final report string ready to post.
    """
    all_candidates = []
    for candidates in sections.values():
        all_candidates.extend(candidates)

    # Stage 1 — enrich reasons
    reasons = _enrich_reasons(all_candidates, settings) if all_candidates else {}

    # Stage 1 — enrich label synopses (cached; only calls LLM for new labels)
    label_watch_candidates = sections.get("label_watch", [])
    active_labels = list({c.label for c in label_watch_candidates if c.label})
    synopses = _enrich_label_synopses(active_labels, settings, settings.data_dir) if active_labels else {}

    # Build structured prompt for Stage 2
    today = datetime.now(timezone.utc).strftime("%-d %B %Y")
    sections_text = "\n\n".join(filter(None, [
        _format_section_for_prompt("TOP PICKS (strongest taste matches)", sections.get("top_picks", []), reasons),
        _format_label_watch_for_prompt(label_watch_candidates, reasons, synopses),
        _format_section_for_prompt("ARTIST WATCH (new material from artists you play)", sections.get("artist_watch", []), reasons),
        _format_section_for_prompt("WILDCARDS (interesting outliers worth a listen)", sections.get("wildcards", []), reasons),
    ]))

    system = (
        "You write a weekly music discovery report for a DJ's Discord channel #music-research. "
        f"{_DJ_CONTEXT} "
        "Write in a concise, knowledgeable tone — like a respected record shop selector. "
        "Format for Discord markdown. Use ** for bold, ## for section headers with emojis. "
        "Exact track format: **Artist — Title** [Label] [Source] → [Listen](<url>) "
        "Each track in the input has a [SOURCE:name] tag — render it as [name] in the track line, "
        "immediately after the label bracket and before the →. "
        "Rules: wrap every URL in angle brackets inside the link ([text](<url>)) to suppress Discord embeds. "
        "Never output bare URLs. Never repeat the same URL twice for one track. "
        "Section headers: ## 🔺 Top Picks, ## 🏷️ Label Watch, ## 👁️ Artist Watch, ## 🃏 Wildcards. "
        "Omit a section header entirely if that section has no tracks. "
        "In the Label Watch section, the input groups tracks by label using [LABEL: name] and [SYNOPSIS: text] tags. "
        "For each label group: render the label name as a bold sub-header (e.g. **Ilian Tape**), "
        "then the synopsis as an italicised line underneath, then the tracks for that label. "
        "Quality over quantity. Do not add tracks that aren't in the input. "
        "End after the last track section — do not add a summary or stats section."
    )

    prompt = (
        f"Write the weekly music discovery report for {today} (Report ID: {report_id}).\n\n"
        f"{sections_text}\n\n"
        "Format the full Discord report with ## section headers (with emojis), bold artist names, "
        "and track links as [Listen](<url>)."
    )

    recommended_count = sum(len(v) for v in sections.values())
    footer = _build_footer(report_id, stats, recommended_count)

    logger.info("[report] Calling Stage 2 for report generation")
    try:
        report = call_stage2(prompt, system, settings)
        report = _sanitize_report(report)
        report = report.rstrip() + "\n\n" + footer
        logger.info(f"[report] Report generated — {len(report)} chars")
        return report
    except Exception as e:
        logger.error(f"[report] Stage 2 failed: {e}")
        return _fallback_report(sections, reasons, report_id, today, stats, synopses)


def generate_mix_prep_report(
    sections: dict[str, list[Candidate]],
    report_id: str,
    stats: dict,
    genre: str,
    settings,
) -> str:
    """
    Generate a Discord-formatted mix-prep report focused on a single genre.
    Uses the same two-stage LLM pipeline as generate_report but with genre-aware prompts.
    """
    all_candidates = []
    for candidates in sections.values():
        all_candidates.extend(candidates)

    reasons = _enrich_reasons(all_candidates, settings) if all_candidates else {}

    today = datetime.now(timezone.utc).strftime("%-d %B %Y")
    sections_text = "\n\n".join(filter(None, [
        _format_section_for_prompt(f"TOP PICKS (best {genre} tracks for the mix)", sections.get("top_picks", []), reasons),
        _format_section_for_prompt("DEEP CUTS (deeper selections worth exploring)", sections.get("deep_cuts", []), reasons),
    ]))

    system = (
        f"You write a mix preparation report for a DJ building a {genre} mix. "
        f"{_DJ_CONTEXT} "
        "Write in a concise, knowledgeable tone — like a respected record shop selector. "
        "Format for Discord markdown. Use ** for bold, ## for section headers with emojis. "
        "Exact track format: **Artist — Title** [Label] → [Listen](<url>) "
        "Rules: wrap every URL in angle brackets inside the link ([text](<url>)) to suppress Discord embeds. "
        "Never output bare URLs. Never repeat the same URL twice for one track. "
        f"Section headers: ## 🔺 Top Picks ({genre}), ## 🎧 Deep Cuts. "
        "Quality over quantity. Do not add tracks that aren't in the input. "
        "End after the last track section — do not add a summary or stats section."
    )

    prompt = (
        f"Write the {genre} mix preparation report for {today} (Report ID: {report_id}).\n\n"
        f"{sections_text}\n\n"
        "Format the full Discord report with ## section headers (with emojis), bold artist names, "
        "and track links as [Listen](<url>)."
    )

    header = _build_mix_prep_header(report_id, today, genre)
    footer = _build_footer(report_id, stats)

    logger.info(f"[report] Calling Stage 2 for mix-prep report — genre: {genre}")
    try:
        report = call_stage2(prompt, system, settings)
        report = _sanitize_report(report)
        report = header + "\n\n" + report.rstrip() + "\n\n" + footer
        logger.info(f"[report] Mix-prep report generated — {len(report)} chars")
        return report
    except Exception as e:
        logger.error(f"[report] Stage 2 failed: {e}")
        return _fallback_mix_prep_report(sections, reasons, report_id, today, genre, stats)


def _build_mix_prep_header(report_id: str, today: str, genre: str) -> str:
    display_genre = genre.upper() if genre == "ukg" else genre.replace("-", " ").title()
    return "\n".join([
        f"🎛️ {display_genre} Mix Prep Report",
        f"Report ID: {report_id}",
        f"Date: {today}",
    ])


def _fallback_mix_prep_report(
    sections: dict[str, list[Candidate]],
    reasons: dict[str, str],
    report_id: str,
    today: str,
    genre: str,
    stats: dict,
) -> str:
    lines = [_build_mix_prep_header(report_id, today, genre), ""]
    section_labels = {
        "top_picks": f"## 🔺 Top Picks ({genre})",
        "deep_cuts": "## 🎧 Deep Cuts",
    }
    for key, header in section_labels.items():
        candidates = sections.get(key, [])
        if not candidates:
            continue
        lines.append(header)
        for c in candidates:
            label_str = f" [{c.label}]" if c.label else ""
            source_str = f" [{c.source.title()}]" if c.source else ""
            link_str = f" → [Listen](<{c.link}>)" if c.link else ""
            lines.append(f"**{c.artist} — {c.title}**{label_str}{source_str}{link_str}")
        lines.append("")
    lines.append(_build_footer(report_id, stats))
    return _sanitize_report("\n".join(lines))


def _fallback_report(
    sections: dict[str, list[Candidate]],
    reasons: dict[str, str],
    report_id: str,
    today: str,
    stats: dict,
    synopses: dict[str, str] | None = None,
) -> str:
    """Plain-text fallback report if Stage 2 fails."""
    lines = [f"**TuneFinder — {today} ({report_id})**\n"]
    section_labels = {
        "top_picks": "## 🔺 Top Picks",
        "artist_watch": "## 👁️ Artist Watch",
        "wildcards": "## 🃏 Wildcards",
    }
    for key, header in section_labels.items():
        candidates = sections.get(key, [])
        if not candidates:
            continue
        lines.append(header)
        for c in candidates:
            label_str = f" [{c.label}]" if c.label else ""
            source_str = f" [{c.source.title()}]" if c.source else ""
            link_str = f" → [Listen](<{c.link}>)" if c.link else ""
            lines.append(f"**{c.artist} — {c.title}**{label_str}{source_str}{link_str}")
        lines.append("")

    label_watch = sections.get("label_watch", [])
    if label_watch:
        lines.append("## 🏷️ Label Watch")
        by_label: dict[str, list[Candidate]] = defaultdict(list)
        for c in label_watch:
            by_label[c.label or ""].append(c)
        for label_name, label_candidates in by_label.items():
            if label_name:
                lines.append(f"**{label_name}**")
                synopsis = (synopses or {}).get(label_name, "")
                if synopsis:
                    lines.append(f"*{synopsis}*")
            for c in label_candidates:
                # label already shown as sub-header above — omit from track line
                source_str = f" [{c.source.title()}]" if c.source else ""
                link_str = f" → [Listen](<{c.link}>)" if c.link else ""
                lines.append(f"**{c.artist} — {c.title}**{source_str}{link_str}")
        lines.append("")

    recommended_count = sum(len(v) for v in sections.values())
    lines.append(_build_footer(report_id, stats, recommended_count))
    return _sanitize_report("\n".join(lines))
