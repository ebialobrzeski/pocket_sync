"""Derived, human-friendly files generated from `raw.json`."""

from __future__ import annotations

from datetime import tzinfo
from typing import Any

from .models import RecordingDetails


def fmt_ts(seconds: float | None) -> str:
    total = int(seconds or 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _header(rec: RecordingDetails, tz: tzinfo, folder: str | None) -> list[str]:
    lines = [f"# {rec.title or 'Untitled'}", ""]
    meta = []
    if rec.started_at:
        meta.append(rec.started_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"))
    if rec.duration:
        meta.append(fmt_ts(rec.duration))
    if folder:
        meta.append(f"folder: {folder}")
    if rec.tags:
        meta.append("tags: " + ", ".join(t.name or t.id for t in rec.tags))
    if meta:
        lines += ["_" + " · ".join(meta) + "_", ""]
    return lines


def transcript_json(rec: RecordingDetails) -> dict[str, Any] | None:
    if rec.transcript is None:
        return None
    return {
        "recording_id": rec.id,
        "metadata": rec.transcript.metadata,
        "segments": [
            {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text}
            for s in rec.transcript.segments
        ],
        "text": rec.transcript.text,
    }


def transcript_md(rec: RecordingDetails, tz: tzinfo, folder: str | None = None) -> str | None:
    if rec.transcript is None:
        return None
    lines = _header(rec, tz, folder)
    if rec.transcript.segments:
        prev_speaker: str | None = None
        for seg in rec.transcript.segments:
            text = seg.text.strip()
            if not text:
                continue
            speaker = f" **{seg.speaker}**" if seg.speaker and seg.speaker != prev_speaker else ""
            lines.append(f"`[{fmt_ts(seg.start)}]`{speaker} {text}")
            lines.append("")
            prev_speaker = seg.speaker
    elif rec.transcript.text:
        lines += [rec.transcript.text.strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def summary_md(rec: RecordingDetails, tz: tzinfo, folder: str | None = None) -> str | None:
    summaries = [(key, s) for key, s in rec.ordered_summarizations() if s.markdown]
    if not summaries:
        return None
    lines = _header(rec, tz, folder)
    multiple = len(summaries) > 1
    for i, (_key, s) in enumerate(summaries, start=1):
        if multiple:
            when = f" ({s.created_at.astimezone(tz):%Y-%m-%d %H:%M})" if s.created_at else ""
            lines += ["---", "", f"<!-- summarization {i} of {len(summaries)}{when} -->", ""]
        lines += [(s.markdown or "").strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def actions_json(rec: RecordingDetails) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, s in rec.ordered_summarizations():
        for action in s.actions:
            out.append({"summarization_id": s.summarization_id or key, **action})
    return out
