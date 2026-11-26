from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from infrastructure_fix.target_resolver import TargetResolver, TargetScope, TargetVerificationError


def _resolver():
    scope = TargetScope("123456789012", "us-east-1", "test-group", "Environment", "simulation")
    sts = Mock()
    sts.get_caller_identity.return_value = {"Account": scope.account_id}
    auto = Mock()
    auto.meta = SimpleNamespace(region_name=scope.region)
    auto.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [{
        "AutoScalingGroupName": "test-group",
        "AutoScalingGroupARN": "arn:aws:autoscaling:us-east-1:123456789012:autoScalingGroup:uuid:autoScalingGroupName/test-group",
        "Tags": [{"Key": "Environment", "Value": "simulation"}],
        "Instances": [{"InstanceId": "i-0123456789abcdef0", "LifecycleState": "InService"}],
        "TargetGroupARNs": ["target-group-arn"],
    }]}
    return TargetResolver(scope, sts, auto)


def test_valid_target_resolves_before_agent_invocation():
    resolver = _resolver()
    result = resolver.resolve("i-0123456789abcdef0")
    assert result.group_name == "test-group"


def test_invalid_hint_does_not_call_aws():
    resolver = _resolver()
    with TestCase().assertRaises(TargetVerificationError):
        resolver.resolve("app-server")
    resolver.sts.get_caller_identity.assert_not_called()


def test_wrong_account_and_tag_are_rejected():
    resolver = _resolver()
    resolver.sts.get_caller_identity.return_value = {"Account": "999999999999"}
    try:
        resolver.resolve("i-0123456789abcdef0")
        assert False, "wrong account must be rejected"
    except TargetVerificationError:
        pass
    resolver = _resolver()
    resolver.autoscaling.describe_auto_scaling_groups.return_value["AutoScalingGroups"][0]["Tags"] = []
    try:
        resolver.resolve("i-0123456789abcdef0")
        assert False, "missing test tag must be rejected"
    except TargetVerificationError:
        pass


def test_wrong_group_member_and_region_are_rejected():
    resolver = _resolver()
    with TestCase().assertRaises(TargetVerificationError):
        resolver.resolve("i-11111111111111111")
    resolver.autoscaling.meta.region_name = "us-west-2"
    with TestCase().assertRaises(TargetVerificationError):
        resolver.resolve("i-0123456789abcdef0")


def test_aws_errors_are_not_treated_as_verified_targets():
    resolver = _resolver()
    resolver.sts.get_caller_identity.side_effect = RuntimeError("sensitive details")
    with TestCase().assertRaises(RuntimeError):
        resolver.resolve("i-0123456789abcdef0")
