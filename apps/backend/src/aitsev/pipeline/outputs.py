"""Structured outputs the models must return (validated with Pydantic)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Plan(BaseModel):
    summary: str = Field(min_length=1, max_length=4000)
    steps: list[str] = Field(min_length=1, max_length=40)
    files_to_change: list[str] = Field(default_factory=list, max_length=200)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=40)
    risks: list[str] = Field(default_factory=list, max_length=40)
    complexity: Literal["small", "normal", "large"] = "normal"

    @field_validator("complexity", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class Review(BaseModel):
    score: float = Field(ge=0, le=10)
    verdict: Literal["approve", "concerns", "reject"]
    issues: list[str] = Field(default_factory=list, max_length=50)
    strengths: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("verdict", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class JudgeDecision(BaseModel):
    winner: str = Field(min_length=1, description="Label of the winning candidate, e.g. 'B'")
    rationale: str = Field(min_length=1, max_length=4000)

    @field_validator("winner", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value
