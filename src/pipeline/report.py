"""
Report generator — two-stage LLM pipeline.

Stage 1 (cheap, fast):
  Enriches the reason field for each shortlisted candidate using the Stage 1
  cascade chain (Mistral → fallback). One batch call covering all sections.
  Falls back to signal-derived reasons if Stage 1 fails.

Stage 2 (Anthropic Sonnet):
  Writes the full Discord-formatted weekly report from the enriched candidates.
  One call per run.
"""
import json
from datetime import datetime, timezone

from src.llm import call_stage1, call_stage2
from src.logger import get_logger
from src.models import Candidate

logger = get_logger(__name__)

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
        raw = call_stage1(prompt, system, settings)
        # Extract JSON from response — handle markdown code blocks
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
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
# Stage 2 — full report
# ---------------------------------------------------------------------------

def _format_section_for_prompt(label: str, candidates: list[Candidate], reasons: dict[str, str]) -> str:
    if not candidates:
        return ""
    lines = [f"{label}:"]
    for c in candidates:
        key = f"{c.artist.lower().strip()}||{c.title.lower().strip()}"
        reason = reasons.get(key) or c.primary_reason or "Interesting new release."
        label_str = f" [{c.label}]" if c.label else ""
        link_str = f" {c.link}" if c.link else ""
        lines.append(f"  - {c.artist} — {c.title}{label_str} | {reason}{link_str}")
    return "\n".join(lines)


def generate_report(
    sections: dict[str, list[Candidate]],
    report_id: str,
    stats: dict,
    settings,
) -> str:
    """
    Generate the full Discord-formatted weekly report.
    Calls Stage 1 to enrich reasons, then Stage 2 to write the report.
    Returns the final report string ready to post.
    """
    all_candidates = []
    for candidates in sections.values():
        all_candidates.extend(candidates)

    # Stage 1 — enrich reasons
    reasons = _enrich_reasons(all_candidates, settings) if all_candidates else {}

    # Build structured prompt for Stage 2
    today = datetime.now(timezone.utc).strftime("%-d %B %Y")
    sections_text = "\n\n".join(filter(None, [
        _format_section_for_prompt("TOP PICKS (strongest taste matches)", sections.get("top_picks", []), reasons),
        _format_section_for_prompt("LABEL WATCH (active labels in your scene)", sections.get("label_watch", []), reasons),
        _format_section_for_prompt("ARTIST WATCH (new material from artists you play)", sections.get("artist_watch", []), reasons),
        _format_section_for_prompt("WILDCARDS (interesting outliers worth a listen)", sections.get("wildcards", []), reasons),
    ]))

    stats_text = (
        f"Sources fetched: {stats.get('sources_fetched', '?')}\n"
        f"Raw candidates: {stats.get('raw_count', '?')}\n"
        f"After dedup: {stats.get('after_dedup', '?')}\n"
        f"After known-track filter: {stats.get('after_known', '?')}\n"
        f"After history filter: {stats.get('after_history', '?')}\n"
        f"Report ID: {report_id}"
    )

    system = (
        "You write a weekly music discovery report for a DJ's Discord channel #music-research. "
        f"{_DJ_CONTEXT} "
        "Write in a concise, knowledgeable tone — like a respected record shop selector. "
        "Format for Discord markdown. Use ** for bold, ## for section headers. "
        "Each track on its own line. Include all track links as [Listen →](url). "
        "Max 2 lines per track. Quality over quantity. Do not add tracks that aren't in the input."
    )

    prompt = (
        f"Write the weekly music discovery report for {today} (Report ID: {report_id}).\n\n"
        f"{sections_text}\n\n"
        f"PROCESSING SUMMARY:\n{stats_text}\n\n"
        "Format the full Discord report with ## section headers, bold artist names, "
        "track links, and a ## Processing Summary section at the end."
    )

    logger.info("[report] Calling Stage 2 (Anthropic) for report generation")
    try:
        report = call_stage2(prompt, system, settings)
        logger.info(f"[report] Report generated — {len(report)} chars")
        return report
    except Exception as e:
        logger.error(f"[report] Stage 2 failed: {e}")
        return _fallback_report(sections, reasons, report_id, today, stats)


def _fallback_report(
    sections: dict[str, list[Candidate]],
    reasons: dict[str, str],
    report_id: str,
    today: str,
    stats: dict,
) -> str:
    """Plain-text fallback report if Stage 2 fails."""
    lines = [f"**Music Finder — {today} ({report_id})**\n"]
    section_labels = {
        "top_picks": "## Top Picks",
        "label_watch": "## Label Watch",
        "artist_watch": "## Artist Watch",
        "wildcards": "## Wildcards",
    }
    for key, header in section_labels.items():
        candidates = sections.get(key, [])
        if not candidates:
            continue
        lines.append(header)
        for c in candidates:
            sk = f"{c.artist.lower().strip()}||{c.title.lower().strip()}"
            reason = reasons.get(sk) or c.primary_reason or ""
            label_str = f" [{c.label}]" if c.label else ""
            link_str = f" [Listen →]({c.link})" if c.link else ""
            lines.append(f"**{c.artist}** — {c.title}{label_str} — {reason}{link_str}")
        lines.append("")

    lines.append("## Processing Summary")
    lines.append(f"Report ID: {report_id} | Sources: {stats.get('sources_fetched', '?')} | "
                 f"Candidates: {stats.get('after_history', '?')}")
    return "\n".join(lines)
