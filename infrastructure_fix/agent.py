"""Read-only infrastructure investigator. It never performs a remediation."""
import json
import os

from langchain_core.messages import HumanMessage

from infrastructure_fix.evidence import EvidenceCollector
from infrastructure_fix.evidence_session import EvidenceSession
from infrastructure_fix.investigation import build_investigation_agent
from infrastructure_fix.host_diagnostics import HostDiagnostics
from infrastructure_fix.models import InfrastructureRequest, InvestigationResult
from infrastructure_fix.onprem import OnPremEvidenceCollector, OnPremScope, OnPremTargetResolver
from infrastructure_fix.policy import assess_rate_limit_candidate, assess_scale_out_candidate
from infrastructure_fix.target_resolver import TargetResolver, TargetScope, TargetVerificationError
from observability.agent_trace import generation_span, log_agent_event, trace_span, update_generation_usage
from observability.token_usage import current_usage, track_usage, usage_config


def _scope_from_env():
    return TargetScope(
        account_id=os.getenv("INFRA_AWS_ACCOUNT_ID", "").strip(),
        region=os.getenv("INFRA_AWS_REGION", "").strip(),
        group_name=os.getenv("INFRA_ASG_NAME", "").strip(),
        tag_key=os.getenv("INFRA_ASG_TAG_KEY", "").strip(),
        tag_value=os.getenv("INFRA_ASG_TAG_VALUE", "").strip(),
    )


def _clients(scope):
    import boto3
    from botocore.config import Config

    config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2, "mode": "standard"})
    session = boto3.Session(region_name=scope.region)
    return {name: session.client(name, config=config) for name in ("sts", "autoscaling", "elbv2", "cloudwatch", "ssm")}


class InfrastructureFixAgent:
    def __init__(self, *, resolver=None, collector=None, model=None):
        self.resolver, self.collector, self.model = resolver, collector, model

    @track_usage(field="token_usage")
    def execute(self, request: InfrastructureRequest) -> dict:
        result = {"agent": "infrastructure_fix_agent", "incident_id": request.incident_id}
        platform = os.getenv("INFRA_PLATFORM", "aws").strip().lower()
        if platform not in {"aws", "onprem"}:
            return {**result, "status": "INVESTIGATION_UNAVAILABLE", "reason": "Trusted infrastructure platform must be aws or onprem."}
        if request.environment != "performance-simulation":
            return {**result, "status": "REVIEW_REQUIRED", "reason": "Only performance-simulation incidents are eligible."}
        if self.resolver is None:
            if os.getenv("INFRA_INVESTIGATION_ENABLED", "false").lower() != "true":
                return {**result, "status": "INVESTIGATION_DISABLED", "reason": f"Read-only {platform} investigation is not enabled."}
            try:
                if platform == "aws":
                    scope = _scope_from_env()
                    clients = _clients(scope)
                    resolver = TargetResolver(scope, clients["sts"], clients["autoscaling"])
                    document = os.getenv("INFRA_SSM_DIAGNOSTIC_DOCUMENT_ARN", "").strip()
                    version = os.getenv("INFRA_SSM_DIAGNOSTIC_DOCUMENT_VERSION", "").strip()
                    diagnostics = HostDiagnostics(clients["ssm"], document, version) if document and version else None
                    collector = EvidenceCollector(clients["autoscaling"], clients["elbv2"], clients["cloudwatch"], diagnostics)
                else:
                    scope = OnPremScope(
                        target_id=os.getenv("INFRA_ONPREM_TARGET_ID", "").strip(),
                        host=os.getenv("INFRA_ONPREM_HOST", "").strip(),
                        ssh_user=os.getenv("INFRA_ONPREM_SSH_USER", "").strip(),
                        known_hosts_file=os.getenv("INFRA_ONPREM_KNOWN_HOSTS", "").strip(),
                        endpoint=os.getenv("INFRA_ONPREM_ENDPOINT", "").strip(),
                    )
                    resolver, collector = OnPremTargetResolver(scope), OnPremEvidenceCollector()
                self.resolver, self.collector = resolver, collector
            except Exception as exc:
                return {**result, "status": "INVESTIGATION_UNAVAILABLE", "reason": f"{platform} investigation setup failed ({type(exc).__name__})."}
        try:
            with trace_span(name="infrastructure_investigation", incident_id=request.incident_id,
                            session_id=request.session_id or request.incident_id, agent="infrastructure_fix",
                            input_data={"target_hint_present": bool(request.target_hint), "environment": request.environment}):
                target = self.resolver.resolve(request.target_hint)
                evidence_session = EvidenceSession(target, self.collector, platform)
                process_names = tuple(name.strip() for name in os.getenv("INFRA_APP_PROCESS_NAMES", "").split(",") if name.strip())
                if platform == "aws":
                    required_evidence = {"cpu", "traffic", "group", "target_health", "host_diagnostics"}
                    action = "scale_out_test_group"
                else:
                    required_evidence = {"cpu", "traffic", "rate_limit", "host_diagnostics"}
                    action = "enable_prepared_rate_limit"
                if self.model is None:
                    from config import agent_llm
                    self.model = agent_llm
                model_agent = build_investigation_agent(self.model, evidence_session, platform)
                with trace_span(name="infrastructure_investigation.analysis", incident_id=request.incident_id,
                                session_id=request.session_id or request.incident_id):
                    with generation_span(name="infrastructure_investigation.llm",
                                         model=getattr(self.model, "model_name", "") or getattr(self.model, "model", ""),
                                         session_id=request.session_id or request.incident_id,
                                         metadata={"agent": "infrastructure_fix"}) as generation:
                        response = model_agent.invoke({"messages": [HumanMessage(content=json.dumps({
                            "incident_id": request.incident_id,
                            "incident_description": request.description[:4000],
                            "l2_diagnosis": request.diagnosis,
                            "instruction": "Use the relevant evidence tools and return an evidence-backed assessment.",
                        }, default=str))]}, config={**usage_config(), "recursion_limit": 12})
                        update_generation_usage(generation, current_usage())
                evidence = evidence_session.records()
                log_agent_event(agent="infrastructure_fix", stage="evidence_collected", status="completed",
                                incident_id=request.incident_id, evidence_ids=[e.kind for e in evidence],
                                summary="Read-only tool results collected during investigation.")
                candidate, reason = (assess_scale_out_candidate(evidence, target, process_names) if platform == "aws"
                                     else assess_rate_limit_candidate(evidence, target, process_names))
                analysis = InvestigationResult.model_validate(response.get("structured_response"))
                known = {item.kind for item in evidence if item.status == "available"}
                if not analysis.evidence_ids or not set(analysis.evidence_ids).issubset(known):
                    return {**result, "status": "REVIEW_REQUIRED", "reason": "Model output did not cite valid collected evidence.",
                            "target": target.model_dump(), "evidence": [e.model_dump(mode="json") for e in evidence]}
                # Scaling is only a possible recommendation; this code does not create an approval.
                if candidate and analysis.assessment != "capacity_pressure_possible":
                    candidate, reason = False, "The model assessment does not support capacity pressure."
                if candidate and not required_evidence.issubset(analysis.evidence_ids):
                    candidate, reason = False, "The model assessment did not cite all evidence required by policy."
                return {**result, "status": "INVESTIGATION_COMPLETED", "target": target.model_dump(),
                        "evidence": [e.model_dump(mode="json") for e in evidence], "analysis": analysis.model_dump(),
                        "platform": platform, "candidate_action": action if candidate else None,
                        "scale_out_candidate": candidate if platform == "aws" else False,
                        "candidate_reason": reason,
                        "next_action": "human_review", "approval_created": False}
        except TargetVerificationError as exc:
            return {**result, "status": "REVIEW_REQUIRED", "reason": str(exc)}
        except Exception as exc:
            return {**result, "status": "INVESTIGATION_FAILED", "reason": f"Investigation failed ({type(exc).__name__}); human review required."}
