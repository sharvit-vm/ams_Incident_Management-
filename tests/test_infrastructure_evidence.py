from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from infrastructure_fix.evidence import EvidenceCollector
from infrastructure_fix.models import VerifiedTarget


def _setup():
    target_arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/demo/abcd"
    target = VerifiedTarget(account_id="123456789012", region="us-east-1", instance_id="i-0123456789abcdef0", group_name="demo", group_arn="group-arn", target_group_arns=[target_arn])
    autoscaling, elb, cloudwatch = Mock(), Mock(), Mock()
    autoscaling.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [{"AutoScalingGroupARN": "group-arn", "DesiredCapacity": 1, "MinSize": 1, "MaxSize": 2, "TargetGroupARNs": [target_arn], "Instances": [{"InstanceId": target.instance_id, "LifecycleState": "InService", "HealthStatus": "Healthy"}]}]}
    autoscaling.describe_scaling_activities.return_value = {"Activities": []}
    autoscaling.describe_policies.return_value = {"ScalingPolicies": []}
    elb.describe_target_health.return_value = {"TargetHealthDescriptions": [{"Target": {"Id": target.instance_id}, "TargetHealth": {"State": "healthy"}}]}
    elb.describe_target_groups.return_value = {"TargetGroups": [{"TargetGroupArn": target_arn, "LoadBalancerArns": ["arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/demo/abcd"]}]}
    now = datetime.now(timezone.utc)
    cloudwatch.get_metric_data.return_value = {"MetricDataResults": [{"StatusCode": "Complete", "Timestamps": [now - timedelta(minutes=5 * i) for i in range(4)], "Values": [90, 90, 70, 70]}]}
    return target, autoscaling, elb, cloudwatch


def test_collects_scoped_metrics_and_target_health():
    target, auto, elb, cloudwatch = _setup()
    records = {r.kind: r for r in EvidenceCollector(auto, elb, cloudwatch).collect(target)}
    assert all(r.status == "available" for name, r in records.items() if name != "host_diagnostics")
    assert records["host_diagnostics"].status == "unavailable"
    assert records["target_health"].details["targets"][0]["id"] == target.instance_id
    assert records["latency"].details["statistic"] == "p95"
    assert cloudwatch.get_metric_data.call_count == 5


def test_partial_metrics_and_aws_errors_are_not_fabricated():
    target, auto, elb, cloudwatch = _setup()
    cloudwatch.get_metric_data.return_value = {"MetricDataResults": [{"StatusCode": "PartialData"}]}
    elb.describe_target_health.side_effect = RuntimeError("secret")
    records = {r.kind: r for r in EvidenceCollector(auto, elb, cloudwatch).collect(target)}
    assert records["cpu"].status == "unavailable"
    assert records["traffic"].status == "unavailable"
    assert records["target_health"].status == "unavailable"
    assert "secret" not in str(records["target_health"].details)


def test_stale_metric_is_unavailable():
    target, auto, elb, cloudwatch = _setup()
    cloudwatch.get_metric_data.return_value = {"MetricDataResults": [{"StatusCode": "Complete", "Timestamps": [datetime.now(timezone.utc) - timedelta(minutes=20)], "Values": [90]}]}
    records = {r.kind: r for r in EvidenceCollector(auto, elb, cloudwatch).collect(target)}
    assert records["cpu"].status == "unavailable"
