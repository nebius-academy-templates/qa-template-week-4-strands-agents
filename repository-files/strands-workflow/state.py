"""Small stage results passed between the native Strands graph nodes."""

from typing import Literal

from pydantic import BaseModel, Field

# Statuses the host derives from execution evidence; an agent cannot claim them.
EVIDENCE_STATUSES = frozenset({"VERIFIED", "FAILED", "NOT_VERIFIED"})


class Assessment(BaseModel):
    status: Literal["READY", "BLOCKED", "NEEDS_CLARIFICATION"]
    reason_and_evidence: str
    next_action_or_question: str


class Implementation(BaseModel):
    status: Literal["VERIFIED", "ALREADY_COVERED", "FAILED", "NOT_VERIFIED", "BLOCKED"]
    target: str = ""
    summary: str


class RepairOutcome(BaseModel):
    status: Literal[
        "VERIFIED",
        "PRODUCT_BUG",
        "NEEDS_INVESTIGATION",
        "INFRASTRUCTURE_ISSUE",
        "EXHAUSTED",
        "NOT_VERIFIED",
    ]
    target: str
    summary: str


class Finding(BaseModel):
    check: str
    path: str
    line: int = Field(ge=1)
    issue: str
    impact: str
    required_change: str


class ReviewResult(BaseModel):
    complete: bool
    summary: str
    findings: list[Finding]
    unverified: str = "none"
    question: str = "none"
