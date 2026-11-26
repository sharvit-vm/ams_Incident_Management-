import json
import tempfile
from datetime import datetime, timedelta, timezone
import subprocess
import sys
from unittest import TestCase
from unittest.mock import Mock, patch

from infrastructure_fix.agent import InfrastructureFixAgent
from infrastructure_fix.models import InfrastructureRequest
from infrastructure_fix.onprem import OnPremEvidenceCollector, OnPremScope, OnPremTargetResolver, _run_bounded
from infrastructure_fix.policy import assess_rate_limit_candidate
from infrastructure_fix.target_resolver import TargetVerificationError
from infrastructure_fix.tools import build_investigation_tools
from infrastructure_fix.evidence_session import EvidenceSession


def _payload(target_id="test-host"):
    now = datetime.now(timezone.utc)
    def points(values):
        return [{"timestamp": (now - timedelta(minutes=5 * i)).isoformat(), "value": value} for i, value in enumerate(values)]
    return {"schema_version": 1, "target_id": target_id, "observed_at": now.isoformat(),
            "cpu": {"points": points([95, 90])}, "memory": {"points": points([50, 49])}, "traffic": {"endpoint": "/cpu-heavy", "points": points([180, 180, 100, 100])},
            "host_diagnostics": {"processes": [{"name": "python", "cpu_percent": 80, "cmdline": "secret"}], "services": [{"name": "app.service", "status": "active"}]},
            "rate_limit": {"endpoint": "/cpu-heavy", "prepared": True, "enabled": False}, "logs": {"error_count": 2, "window_minutes": 10}}


def _resolver(path):
    return OnPremTargetResolver(OnPremScope("test-host", "test.example", "amsreader", path, "/cpu-heavy"))


def test_target_scope_and_fixed_read_only_command():
    with tempfile.NamedTemporaryFile() as known:
        resolver = _resolver(known.name)
        with TestCase().assertRaises(TargetVerificationError):
            resolver.resolve("different-host")
        target = resolver.resolve("test-host")
        runner = Mock(return_value=json.dumps(_payload()))
        evidence = OnPremEvidenceCollector(runner=runner).collect(target)
        command = runner.call_args.args[0]
        assert command[-1] == "/usr/local/sbin/ams-infra-diagnostics --json"
        assert command[-2] == "amsreader@test.example"
        assert "StrictHostKeyChecking=yes" in command
        assert all(e.status == "available" for e in evidence)
        assert "secret" not in str(evidence)
        assert assess_rate_limit_candidate(evidence, target, ("python",))[0]


def test_stale_and_mismatched_results_are_unavailable():
    with tempfile.NamedTemporaryFile() as known:
        target = _resolver(known.name).resolve("test-host")
        payload = _payload("other-host")
        runner = Mock(return_value=json.dumps(payload))
        assert all(e.status == "unavailable" for e in OnPremEvidenceCollector(runner=runner).collect(target))
        payload = _payload()
        payload["observed_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        runner.return_value = json.dumps(payload)
        assert all(e.status == "unavailable" for e in OnPremEvidenceCollector(runner=runner).collect(target))


def test_rate_limit_decision_requires_prepared_limiter_and_process():
    with tempfile.NamedTemporaryFile() as known:
        target = _resolver(known.name).resolve("test-host")
        payload = _payload()
        payload["rate_limit"]["enabled"] = True
        runner = Mock(return_value=json.dumps(payload))
        evidence = OnPremEvidenceCollector(runner=runner).collect(target)
        assert not assess_rate_limit_candidate(evidence, target, ("python",))[0]
        payload["rate_limit"]["enabled"] = False
        runner.return_value = json.dumps(payload)
        evidence = OnPremEvidenceCollector(runner=runner).collect(target)
        assert not assess_rate_limit_candidate(evidence, target, ("other",))[0]
        payload["rate_limit"]["endpoint"] = "/other"
        runner.return_value = json.dumps(payload)
        evidence = OnPremEvidenceCollector(runner=runner).collect(target)
        assert not assess_rate_limit_candidate(evidence, target, ("python",))[0]


def test_onprem_tools_return_real_collected_snapshot():
    with tempfile.NamedTemporaryFile() as known:
        target = _resolver(known.name).resolve("test-host")
        runner = Mock(return_value=json.dumps(_payload()))
        session = EvidenceSession(target, OnPremEvidenceCollector(runner=runner), "onprem")
        tools = {t.name: t for t in build_investigation_tools(session, "onprem")}
        assert set(tools) == {"get_server_metrics", "get_endpoint_metrics", "get_process_and_service_summary", "get_rate_limit_state", "get_relevant_logs"}
        assert tools["get_rate_limit_state"].invoke({})["rate_limit"]["details"]["prepared"] is True


def test_onprem_agent_can_only_report_candidate():
    with tempfile.NamedTemporaryFile() as known:
        resolver = _resolver(known.name)
        runner = Mock(return_value=json.dumps(_payload()))
        collector = OnPremEvidenceCollector(runner=runner)
        agent = InfrastructureFixAgent(resolver=resolver, collector=collector, model=Mock())
        request = InfrastructureRequest(incident_id="INC001", environment="performance-simulation", target_hint="test-host")
        model = Mock()
        model.invoke.return_value = {"structured_response": {"assessment": "capacity_pressure_possible", "summary": "Possible endpoint overload", "evidence_ids": ["cpu", "traffic", "host_diagnostics", "rate_limit"]}}
        def build(llm, session, platform):
            model.invoke.side_effect = lambda *args, **kwargs: (session.read("cpu", "traffic", "host_diagnostics", "rate_limit"), {"structured_response": {"assessment": "capacity_pressure_possible", "summary": "Possible endpoint overload", "evidence_ids": ["cpu", "traffic", "host_diagnostics", "rate_limit"]}})[1]
            return model
        with patch.dict("os.environ", {"INFRA_PLATFORM": "onprem", "INFRA_APP_PROCESS_NAMES": "python"}), patch("observability.agent_trace._get_langfuse_client", return_value=None), patch("infrastructure_fix.agent.build_investigation_agent", side_effect=build):
            result = agent.execute(request)
        assert result["candidate_action"] == "enable_prepared_rate_limit"
        assert result["approval_created"] is False
        assert runner.call_count == 1


def test_output_limit_and_timeout_are_enforced_while_process_runs():
    assert _run_bounded([sys.executable, "-c", "print('ok')"], timeout_seconds=5).strip() == "ok"
    with TestCase().assertRaises(ValueError):
        _run_bounded([sys.executable, "-c", "print('x' * 1000000)"], timeout_seconds=5)
    with TestCase().assertRaises(subprocess.TimeoutExpired):
        _run_bounded([sys.executable, "-c", "import time; time.sleep(10)"], timeout_seconds=1)


def test_timestamps_are_normalized_to_utc_before_sorting():
    now = datetime.now(timezone.utc)
    older = now - timedelta(minutes=4)
    newer = now - timedelta(minutes=2)
    from datetime import timezone as tz
    raw = {"points": [{"timestamp": older.astimezone(tz(timedelta(hours=5, minutes=30))).isoformat(), "value": 10},
                      {"timestamp": newer.astimezone(tz(timedelta(hours=-5))).isoformat(), "value": 20}]}
    points = OnPremEvidenceCollector._validate("cpu", raw, now)["points"]
    assert points[0]["value"] == 20
    assert all(datetime.fromisoformat(point["timestamp"]).utcoffset() == timedelta(0) for point in points)


def test_onprem_cpu_and_traffic_periods_must_match():
    with tempfile.NamedTemporaryFile() as known:
        target = _resolver(known.name).resolve("test-host")
        payload = _payload()
        for point in payload["traffic"]["points"]:
            point["timestamp"] = (datetime.fromisoformat(point["timestamp"]) - timedelta(minutes=5)).isoformat()
        runner = Mock(return_value=json.dumps(payload))
        evidence = OnPremEvidenceCollector(runner=runner).collect(target)
        candidate, reason = assess_rate_limit_candidate(evidence, target, ("python",))
        assert candidate is False
        assert "same recent periods" in reason
