from unittest.mock import Mock, patch
from datetime import datetime, timezone

from infrastructure_fix.investigation import build_investigation_agent
from infrastructure_fix.models import EvidenceRecord, VerifiedTarget
from infrastructure_fix.evidence_session import EvidenceSession
from infrastructure_fix.evidence import EvidenceCollector
from infrastructure_fix.tools import build_investigation_tools


def test_agent_exposes_planned_read_only_investigation_tools():
    collector = Mock()
    collector.collect.return_value = []
    with patch("infrastructure_fix.investigation.create_agent") as create:
        build_investigation_agent(Mock(), EvidenceSession(Mock(), collector, "onprem"), "onprem")
    tools = {tool.name: tool for tool in create.call_args.kwargs["tools"]}
    assert set(tools) == {"get_server_metrics", "get_endpoint_metrics", "get_process_and_service_summary", "get_rate_limit_state", "get_relevant_logs"}
    assert tools["get_process_and_service_summary"].invoke({})["host_diagnostics"]["status"] == "unavailable"


def test_tool_returns_collected_group_evidence():
    evidence = [EvidenceRecord(kind="group", source="autoscaling", observed_at=datetime.now(timezone.utc), status="available", details={"desired": 1, "maximum": 2})]
    collector = Mock()
    collector.collect_kind.return_value = evidence[0]
    target = Mock()
    session = EvidenceSession(target, collector, "aws")
    with patch("infrastructure_fix.investigation.create_agent") as create:
        build_investigation_agent(Mock(), session)
    tools = {tool.name: tool for tool in create.call_args.kwargs["tools"]}
    result = tools["get_group_state"].invoke({})["group"]
    assert result["source"] == "autoscaling"
    assert result["details"]["desired"] == 1
    collector.collect_kind.assert_called_once_with(target, "group")
    tools["get_group_state"].invoke({})
    collector.collect_kind.assert_called_once()


def test_aws_tool_queries_cloudwatch_on_demand_for_verified_instance():
    cloudwatch = Mock()
    cloudwatch.get_metric_data.return_value = {"MetricDataResults": [{"StatusCode": "Complete", "Timestamps": [datetime.now(timezone.utc)], "Values": [85.0]}]}
    target = VerifiedTarget(account_id="123456789012", region="us-east-1", instance_id="i-0123456789abcdef0", group_name="demo", group_arn="arn")
    collector = EvidenceCollector(Mock(), Mock(), cloudwatch)
    session = EvidenceSession(target, collector, "aws")
    tools = {item.name: item for item in build_investigation_tools(session, "aws")}
    cloudwatch.get_metric_data.assert_not_called()
    response = tools["get_cpu_and_credits"].invoke({})
    assert response["cpu"]["details"]["points"][0]["value"] == 85.0
    assert cloudwatch.get_metric_data.call_count == 2
    query = cloudwatch.get_metric_data.call_args_list[0].kwargs["MetricDataQueries"][0]
    assert query["MetricStat"]["Metric"]["Dimensions"] == [{"Name": "InstanceId", "Value": target.instance_id}]
    tools["get_cpu_and_credits"].invoke({})
    assert cloudwatch.get_metric_data.call_count == 2
