from infrastructure_fix.models import InfrastructureRequest


class InfrastructureFixAgent:
    """Phase 1 routing endpoint; no AWS access or remediation is performed."""

    def execute(self, request: InfrastructureRequest) -> dict:
        return {
            "status": "INVESTIGATION_NOT_IMPLEMENTED",
            "agent": "infrastructure_fix_agent",
            "incident_id": request.incident_id,
            "target_hint": request.target_hint,
            "environment": request.environment,
            "reason": "Infrastructure investigation and target verification are not implemented yet.",
        }
