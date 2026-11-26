from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class InfrastructureRequest(BaseModel):
    """Incident context for investigation; target_hint must be independently verified."""

    incident_id: str
    diagnosis: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    target_hint: str | None = None
    environment: str | None = None
    session_id: str = ""


class VerifiedTarget(BaseModel):
    account_id: str
    region: str
    instance_id: str
    group_name: str
    group_arn: str
    target_group_arns: list[str] = Field(default_factory=list)


class VerifiedOnPremTarget(BaseModel):
    """An inventory-scoped host; connection details are trusted configuration."""
    platform: Literal["onprem"] = "onprem"
    target_id: str
    host: str
    ssh_user: str
    known_hosts_file: str
    endpoint: str


class EvidenceRecord(BaseModel):
    kind: str
    source: str
    observed_at: datetime
    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class InvestigationResult(BaseModel):
    assessment: Literal["capacity_pressure_possible", "other_cause_possible", "insufficient_evidence"]
    summary: str = Field(max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=10)
    next_checks: list[str] = Field(default_factory=list, max_length=10)
