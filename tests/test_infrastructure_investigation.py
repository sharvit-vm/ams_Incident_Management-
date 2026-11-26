from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from infrastructure_fix.agent import InfrastructureFixAgent
from infrastructure_fix.models import EvidenceRecord, InfrastructureRequest, VerifiedTarget
from infrastructure_fix.policy import assess_scale_out_candidate as _candidate_guard
from infrastructure_fix.target_resolver import TargetVerificationError


def _setup():
    now = datetime.now(timezone.utc)
    target = VerifiedTarget(account_id="123456789012", region="us-east-1", instance_id="i-0123456789abcdef0", group_name="demo", group_arn="arn", target_group_arns=["tg"])
    def rec(kind, details):
        return EvidenceRecord(kind=kind, source="test", observed_at=now, status="available", details=details)
    def points(values):
        return [{"timestamp": (now - timedelta(minutes=5 * i)).isoformat(), "value": value} for i, value in enumerate(values)]
    evidence = [
        rec("group", {"desired": 1, "maximum": 2, "enabled_scaling_policies": 0, "scaling_activity_statuses": []}),
        rec("target_health", {"targets": [{"id": target.instance_id, "state": "healthy"}]}),
        rec("cpu", {"points": points([95, 91])}),
        rec("traffic", {"points": points([200, 180, 100, 100])}),
        rec("host_diagnostics", {"processes": [{"name": "python", "cpu_percent": 85}], "services": []}),
    ]
    request = InfrastructureRequest(incident_id="INC001", environment="performance-simulation", target_hint=target.instance_id)
    return target, evidence, request


def test_candidate_requires_complete_current_evidence():
    target, evidence, _ = _setup()
    assert _candidate_guard(evidence, target, ("python",))[0]
    evidence[2].status = "unavailable"
    assert not _candidate_guard(evidence, target, ("python",))[0]
    evidence[2].status = "available"
    evidence[0].details["enabled_scaling_policies"] = 1
    assert not _candidate_guard(evidence, target, ("python",))[0]


def test_candidate_rejects_gaps_and_concurrent_scaling():
    target, evidence, _ = _setup()
    evidence[3].details["points"][1]["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    assert not _candidate_guard(evidence, target, ("python",))[0]
    target, evidence, _ = _setup()
    evidence[0].details["scaling_activity_statuses"] = ["InProgress"]
    assert not _candidate_guard(evidence, target, ("python",))[0]


def test_candidate_requires_verified_application_process():
    target, evidence, _ = _setup()
    assert not _candidate_guard(evidence, target)[0]
    assert not _candidate_guard(evidence, target, ("other-app",))[0]
    evidence[-1].status = "unavailable"
    assert not _candidate_guard(evidence, target, ("python",))[0]


def test_cpu_and_traffic_must_cover_same_recent_periods():
    target, evidence, _ = _setup()
    for point in evidence[3].details["points"]:
        stamp = datetime.fromisoformat(point["timestamp"])
        point["timestamp"] = (stamp - timedelta(minutes=5)).isoformat()
    candidate, reason = _candidate_guard(evidence, target, ("python",))
    assert candidate is False
    assert "same recent periods" in reason


def test_policy_rejection_reason_is_returned_to_user():
    target, evidence, request = _setup()
    evidence[0].details["enabled_scaling_policies"] = 1
    resolver, collector = Mock(), Mock()
    resolver.resolve.return_value = target
    collector.collect_kind.side_effect = lambda target, kind: next(item for item in evidence if item.kind == kind)
    agent = InfrastructureFixAgent(resolver=resolver, collector=collector, model=Mock())
    model_agent = Mock()
    def build(model, session, platform):
        model_agent.invoke.side_effect = lambda *args, **kwargs: (session.read("cpu", "traffic", "group", "target_health", "host_diagnostics"), {"structured_response": {"assessment": "capacity_pressure_possible", "summary": "test", "evidence_ids": ["cpu", "traffic", "group", "target_health", "host_diagnostics"]}})[1]
        return model_agent
    with patch.dict("os.environ", {"INFRA_APP_PROCESS_NAMES": "python", "INFRA_PLATFORM": "aws"}), patch("observability.agent_trace._get_langfuse_client", return_value=None), patch("infrastructure_fix.agent.build_investigation_agent", side_effect=build):
        result = agent.execute(request)
    assert result["candidate_action"] is None
    assert "scaling policy" in result["candidate_reason"]


def test_disabled_message_reflects_configured_platform():
    request = InfrastructureRequest(incident_id="INC001", environment="performance-simulation")
    with patch.dict("os.environ", {"INFRA_PLATFORM": "onprem", "INFRA_INVESTIGATION_ENABLED": "false"}):
        result = InfrastructureFixAgent().execute(request)
    assert result["status"] == "INVESTIGATION_DISABLED"
    assert "onprem" in result["reason"]


def test_investigator_uses_verified_evidence_without_aws_write():
    target, evidence, request = _setup()
    resolver, collector = Mock(), Mock()
    resolver.resolve.return_value = target
    collector.collect_kind.side_effect = lambda target, kind: next(item for item in evidence if item.kind == kind)
    model_agent = Mock()
    model_agent.invoke.return_value = {"structured_response": {"assessment": "capacity_pressure_possible", "summary": "Capacity pressure is possible.", "evidence_ids": ["cpu", "traffic", "group", "target_health", "host_diagnostics"], "limitations": [], "next_checks": []}}
    agent = InfrastructureFixAgent(resolver=resolver, collector=collector, model=Mock())
    def build(model, session, platform):
        model_agent.invoke.side_effect = lambda *args, **kwargs: (session.read("cpu", "traffic", "group", "target_health", "host_diagnostics"), {"structured_response": {"assessment": "capacity_pressure_possible", "summary": "Capacity pressure is possible.", "evidence_ids": ["cpu", "traffic", "group", "target_health", "host_diagnostics"]}})[1]
        return model_agent
    with patch.dict("os.environ", {"INFRA_APP_PROCESS_NAMES": "python"}), patch("observability.agent_trace._get_langfuse_client", return_value=None), patch("infrastructure_fix.agent.build_investigation_agent", side_effect=build):
        result = agent.execute(request)
    assert result["status"] == "INVESTIGATION_COMPLETED"
    assert result["scale_out_candidate"] is True
    assert result["approval_created"] is False
    resolver.resolve.assert_called_once_with(target.instance_id)
    assert collector.collect_kind.call_count == 5


def test_unknown_citations_and_invalid_target_fail_closed():
    target, evidence, request = _setup()
    resolver, collector, model_agent = Mock(), Mock(), Mock()
    resolver.resolve.return_value = target
    collector.collect_kind.side_effect = lambda target, kind: next(item for item in evidence if item.kind == kind)
    model_agent.invoke.return_value = {"structured_response": {"assessment": "capacity_pressure_possible", "summary": "test", "evidence_ids": ["invented"]}}
    agent = InfrastructureFixAgent(resolver=resolver, collector=collector, model=Mock())
    def build(model, session, platform):
        model_agent.invoke.side_effect = lambda *args, **kwargs: (session.read("cpu"), {"structured_response": {"assessment": "capacity_pressure_possible", "summary": "test", "evidence_ids": ["invented"]}})[1]
        return model_agent
    with patch("observability.agent_trace._get_langfuse_client", return_value=None), patch("infrastructure_fix.agent.build_investigation_agent", side_effect=build):
        result = agent.execute(request)
        assert result["status"] == "REVIEW_REQUIRED"
        resolver.resolve.side_effect = TargetVerificationError("unverified")
        assert agent.execute(request)["status"] == "REVIEW_REQUIRED"


def test_model_exception_is_reported_and_wrong_environment_stops():
    target, evidence, request = _setup()
    resolver, collector = Mock(), Mock()
    resolver.resolve.return_value, collector.collect.return_value = target, evidence
    agent = InfrastructureFixAgent(resolver=resolver, collector=collector, model=Mock())
    model_agent = Mock()
    model_agent.invoke.side_effect = ValueError("private data")
    with patch("observability.agent_trace._get_langfuse_client", return_value=None), patch("infrastructure_fix.agent.build_investigation_agent", return_value=model_agent):
        result = agent.execute(request)
    assert result["status"] == "INVESTIGATION_FAILED"
    assert "private data" not in result["reason"]
    request.environment = "production"
    assert agent.execute(request)["status"] == "REVIEW_REQUIRED"
