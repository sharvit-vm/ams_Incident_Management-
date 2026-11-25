from typing import Any

from pydantic import BaseModel, Field


class InfrastructureRequest(BaseModel):
    """Incident context for investigation; target_hint must be independently verified."""

    incident_id: str
    diagnosis: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    target_hint: str | None = None
    environment: str | None = None
