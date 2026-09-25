"""Pydantic models of Pocket API responses (shapes documented in docs/api-notes.md).

All models accept unknown fields: the API is young and adds fields without notice. `raw.json`
remains the source of truth; these models only drive the derived files.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TERMINAL_PROCESSING_STATES = {"completed", "failed", "error"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Tag(_Model):
    id: str
    name: str | None = None
    color: str | None = None


class RecordingSummary(_Model):
    """Item of `GET /public/recordings`."""

    id: str
    title: str | None = None
    folder_id: str | None = None
    duration: float | None = None
    state: str | None = None
    language: str | None = None
    recording_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    tags: list[Tag] = Field(default_factory=list)

    @property
    def started_at(self) -> datetime | None:
        return self.recording_at or self.created_at

    @property
    def is_completed(self) -> bool:
        return self.state == "completed"


class Segment(_Model):
    start: float | None = None
    end: float | None = None
    text: str = ""
    speaker: str | None = None


class Transcript(_Model):
    text: str | None = None
    segments: list[Segment] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SummaryBody(_Model):
    markdown: str | None = None


class ActionItems(_Model):
    actions: list[dict[str, Any]] | None = None
    # The onboarding recording uses `actionItems.actionItems` instead of `actionItems.actions`.
    action_items: list[dict[str, Any]] | None = Field(default=None, alias="actionItems")

    @property
    def items(self) -> list[dict[str, Any]]:
        return self.actions or self.action_items or []


class SummarizationV2(_Model):
    summary: SummaryBody | None = None
    action_items: ActionItems | None = Field(default=None, alias="actionItems")


class Summarization(_Model):
    id: str | None = None
    summarization_id: str | None = Field(default=None, alias="summarizationId")
    processing_status: str | None = Field(default=None, alias="processingStatus")
    v2: SummarizationV2 | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")

    @property
    def markdown(self) -> str | None:
        return self.v2.summary.markdown if self.v2 and self.v2.summary else None

    @property
    def actions(self) -> list[dict[str, Any]]:
        return self.v2.action_items.items if self.v2 and self.v2.action_items else []


class RecordingDetails(RecordingSummary):
    """`data` of `GET /public/recordings/{id}`."""

    transcript: Transcript | None = None
    summarizations: dict[str, Summarization] | None = None

    def ordered_summarizations(self) -> list[tuple[str, Summarization]]:
        items = list((self.summarizations or {}).items())
        return sorted(items, key=lambda kv: (kv[1].created_at is None, kv[1].created_at or 0, kv[0]))

    @property
    def is_processing_complete(self) -> bool:
        """True when Pocket has nothing left to generate for this recording."""
        if not self.is_completed:
            return False
        return all(
            (s.processing_status or "completed") in TERMINAL_PROCESSING_STATES
            for s in (self.summarizations or {}).values()
        )


class AudioUrl(_Model):
    signed_url: str
    expires_in: int | None = None
    expires_at: datetime | None = None


class Folder(_Model):
    id: str
    name: str | None = None
    kind: str | None = None
    parent_folder_id: str | None = None
    children: list[Folder] = Field(default_factory=list)


def flatten_folders(folders: list[Folder], prefix: str = "") -> dict[str, str]:
    """Map folder id -> "Parent/Child" path."""
    out: dict[str, str] = {}
    for f in folders:
        path = f"{prefix}/{f.name or f.id}" if prefix else (f.name or f.id)
        out[f.id] = path
        out.update(flatten_folders(f.children, path))
    return out
