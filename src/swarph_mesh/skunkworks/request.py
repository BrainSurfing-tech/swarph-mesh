"""Skunkworks request: what a cell sends to ask for a run. Card and seed are required."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class SkunkworksRequest(BaseModel):
    requesting_swarm: str = Field(min_length=1, description="The requesting cell.")
    persona: str = Field(min_length=1, description="Persona pack name, e.g. 'swarph'.")
    seed: str = Field(
        min_length=1, description="The question or direction to ideate from."
    )
    card_id: int = Field(ge=1, description="Board card the run reports to.")
    blocker_question: Optional[str] = Field(
        default=None, description="Concrete blocker, shown to ideation."
    )
    context_md_paths: List[str] = Field(
        default_factory=list,
        description="Files shown to the architect only (never to ideation or research).",
    )

    @field_validator("seed", "requesting_swarm", "persona")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v
