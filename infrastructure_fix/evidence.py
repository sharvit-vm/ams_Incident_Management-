"""Bounded read-only AWS evidence collection for one previously verified target."""
from datetime import datetime, timedelta, timezone
import math

from infrastructure_fix.models import EvidenceRecord, VerifiedTarget


def _record(kind, source, status, details):
    return EvidenceRecord(kind=kind, source=source, status=status,
                          observed_at=datetime.now(timezone.utc), details=details)


class EvidenceCollector:
    def __init__(self, autoscaling, elbv2, cloudwatch, host_diagnostics=None):
        self.autoscaling, self.elbv2, self.cloudwatch = autoscaling, elbv2, cloudwatch
        self.host_diagnostics = host_diagnostics

    def collect(self, target: VerifiedTarget) -> list[EvidenceRecord]:
        return [self.collect_kind(target, kind) for kind in (
            "group", "target_health", "cpu", "cpu_credits", "traffic", "latency", "target_5xx", "host_diagnostics"
        )]

    def collect_kind(self, target: VerifiedTarget, kind: str) -> EvidenceRecord:
        """Fetch one approved evidence type for a verified target."""
        callbacks = {"group": self._group, "target_health": self._health,
                     "cpu": self._cpu, "cpu_credits": self._credits, "traffic": self._traffic,
                     "latency": self._latency, "target_5xx": self._target_5xx}
        if kind == "host_diagnostics":
            if self.host_diagnostics is None:
                return _record(kind, "ssm", "unavailable", {"reason": "fixed diagnostic document is not configured"})
            callback = self.host_diagnostics.collect
        else:
            callback = callbacks.get(kind)
        if callback is None:
            raise ValueError("Unsupported evidence type")
        try:
            return callback(target)
        except Exception as exc:
            return _record(kind, "aws", "unavailable", {"error_type": type(exc).__name__})

    def _group(self, target):
        groups = self.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[target.group_name]).get("AutoScalingGroups", [])
        if len(groups) != 1 or groups[0].get("AutoScalingGroupARN") != target.group_arn:
            return _record("group", "autoscaling", "unavailable", {"reason": "group identity changed"})
        group = groups[0]
        if sorted(group.get("TargetGroupARNs", [])) != sorted(target.target_group_arns):
            return _record("group", "autoscaling", "unavailable", {"reason": "attached target groups changed"})
        if not any(i.get("InstanceId") == target.instance_id and i.get("LifecycleState") == "InService" for i in group.get("Instances", [])):
            return _record("group", "autoscaling", "unavailable", {"reason": "instance no longer in service in verified group"})
        activities = self.autoscaling.describe_scaling_activities(AutoScalingGroupName=target.group_name, MaxRecords=10).get("Activities", [])
        policies = self.autoscaling.describe_policies(AutoScalingGroupName=target.group_name, MaxRecords=100)
        if policies.get("NextToken"):
            return _record("group", "autoscaling", "unavailable", {"reason": "scaling policies exceed bounded query"})
        return _record("group", "autoscaling", "available", {
            "desired": group.get("DesiredCapacity"), "minimum": group.get("MinSize"),
            "maximum": group.get("MaxSize"),
            "instances": [{"id": i.get("InstanceId"), "state": i.get("LifecycleState"), "health": i.get("HealthStatus")} for i in group.get("Instances", [])],
            "scaling_activity_statuses": [a.get("StatusCode") for a in activities],
            "enabled_scaling_policies": sum(p.get("Enabled", True) for p in policies.get("ScalingPolicies", [])),
        })

    def _health(self, target):
        if len(target.target_group_arns) != 1:
            return _record("target_health", "elbv2", "unavailable", {"reason": "expected exactly one attached target group"})
        arn = target.target_group_arns[0]
        expected = f"arn:aws:elasticloadbalancing:{target.region}:{target.account_id}:targetgroup/"
        if not arn.startswith(expected):
            return _record("target_health", "elbv2", "unavailable", {"reason": "target group is outside verified scope"})
        result = self.elbv2.describe_target_health(TargetGroupArn=arn)
        return _record("target_health", "elbv2", "available", {"targets": [
            {"id": item.get("Target", {}).get("Id"), "state": item.get("TargetHealth", {}).get("State"),
             "reason": item.get("TargetHealth", {}).get("Reason")}
            for item in result.get("TargetHealthDescriptions", [])]})

    def _metric(self, namespace, name, dimensions, statistic, period=300):
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=30)
        response = self.cloudwatch.get_metric_data(
            MetricDataQueries=[{"Id": "metric", "MetricStat": {"Metric": {"Namespace": namespace,
                "MetricName": name, "Dimensions": dimensions}, "Period": period, "Stat": statistic}, "ReturnData": True}],
            StartTime=start, EndTime=end, ScanBy="TimestampDescending", MaxDatapoints=100,
        )
        values = response.get("MetricDataResults", [])
        if len(values) != 1 or values[0].get("StatusCode") != "Complete" or response.get("NextToken"):
            return {"reason": "metric data incomplete"}, False
        metric = values[0]
        timestamps, measurements = metric.get("Timestamps", []), metric.get("Values", [])
        if len(timestamps) != len(measurements):
            return {"reason": "metric data malformed"}, False
        points = []
        for timestamp, value in zip(timestamps, measurements):
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or not isinstance(value, (int, float)) or not math.isfinite(value):
                return {"reason": "metric data malformed"}, False
            if start <= timestamp <= end:
                points.append({"timestamp": timestamp.isoformat(), "value": value})
        points.sort(key=lambda p: p["timestamp"], reverse=True)
        fresh = bool(points) and datetime.fromisoformat(points[0]["timestamp"]) >= end - timedelta(minutes=10)
        return {"metric": name, "statistic": statistic, "period_seconds": period,
                "window_start": start.isoformat(), "window_end": end.isoformat(), "points": points}, fresh

    def _cpu(self, target):
        details, available = self._metric("AWS/EC2", "CPUUtilization", [{"Name": "InstanceId", "Value": target.instance_id}], "Average")
        return _record("cpu", "cloudwatch", "available" if available else "unavailable", details)

    def _credits(self, target):
        details, available = self._metric("AWS/EC2", "CPUCreditBalance", [{"Name": "InstanceId", "Value": target.instance_id}], "Average")
        return _record("cpu_credits", "cloudwatch", "available" if available else "unavailable", details)

    def _traffic(self, target):
        if len(target.target_group_arns) != 1:
            return _record("traffic", "cloudwatch", "unavailable", {"reason": "expected one target group"})
        arn = target.target_group_arns[0]
        expected = f"arn:aws:elasticloadbalancing:{target.region}:{target.account_id}:targetgroup/"
        if not arn.startswith(expected):
            return _record("traffic", "cloudwatch", "unavailable", {"reason": "target group is outside verified scope"})
        details, available = self._metric("AWS/ApplicationELB", "RequestCountPerTarget", [{"Name": "TargetGroup", "Value": arn.split(":", 5)[-1]}], "Sum")
        return _record("traffic", "cloudwatch", "available" if available else "unavailable", details)

    def _alb_metric(self, target, kind, metric_name, statistic):
        if len(target.target_group_arns) != 1:
            return _record(kind, "cloudwatch", "unavailable", {"reason": "expected one target group"})
        arn = target.target_group_arns[0]
        expected = f"arn:aws:elasticloadbalancing:{target.region}:{target.account_id}:targetgroup/"
        if not arn.startswith(expected):
            return _record(kind, "cloudwatch", "unavailable", {"reason": "target group is outside verified scope"})
        groups = self.elbv2.describe_target_groups(TargetGroupArns=[arn]).get("TargetGroups", [])
        if len(groups) != 1 or groups[0].get("TargetGroupArn") != arn:
            return _record(kind, "elbv2", "unavailable", {"reason": "target group not uniquely found"})
        balancers = groups[0].get("LoadBalancerArns", [])
        expected_lb = f"arn:aws:elasticloadbalancing:{target.region}:{target.account_id}:loadbalancer/app/"
        if len(balancers) != 1 or not balancers[0].startswith(expected_lb):
            return _record(kind, "elbv2", "unavailable", {"reason": "expected one in-scope application load balancer"})
        dimensions = [{"Name": "TargetGroup", "Value": arn.split(":", 5)[-1]},
                      {"Name": "LoadBalancer", "Value": balancers[0].split("loadbalancer/", 1)[1]}]
        details, available = self._metric("AWS/ApplicationELB", metric_name, dimensions, statistic)
        return _record(kind, "cloudwatch", "available" if available else "unavailable", details)

    def _latency(self, target):
        return self._alb_metric(target, "latency", "TargetResponseTime", "p95")

    def _target_5xx(self, target):
        # This metric may be absent when no target errors occur; absence is not assumed to mean zero.
        return self._alb_metric(target, "target_5xx", "HTTPCode_Target_5XX_Count", "Sum")
