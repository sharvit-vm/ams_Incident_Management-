"""Read-only target verification. Incident hints never define the allowed scope."""
import re
from dataclasses import dataclass

from infrastructure_fix.models import VerifiedTarget


class TargetVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class TargetScope:
    account_id: str
    region: str
    group_name: str
    tag_key: str
    tag_value: str

    def __post_init__(self):
        if not all((self.account_id, self.region, self.group_name, self.tag_key, self.tag_value)):
            raise TargetVerificationError("A complete trusted target scope is required")


class TargetResolver:
    def __init__(self, scope: TargetScope, sts, autoscaling):
        self.scope = scope
        self.sts = sts
        self.autoscaling = autoscaling

    def resolve(self, target_hint: str | None) -> VerifiedTarget:
        if not target_hint or not re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", target_hint):
            raise TargetVerificationError("Target hint must be an exact EC2 instance ID")
        if self.sts.get_caller_identity().get("Account") != self.scope.account_id:
            raise TargetVerificationError("AWS account does not match the configured scope")
        if self.autoscaling.meta.region_name != self.scope.region:
            raise TargetVerificationError("AWS region does not match the configured scope")
        response = self.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[self.scope.group_name])
        groups = response.get("AutoScalingGroups", [])
        if len(groups) != 1:
            raise TargetVerificationError("Configured Auto Scaling group was not uniquely found")
        group = groups[0]
        if group.get("AutoScalingGroupName") != self.scope.group_name:
            raise TargetVerificationError("Auto Scaling group is outside the configured scope")
        tags = {tag.get("Key"): tag.get("Value") for tag in group.get("Tags", [])}
        if tags.get(self.scope.tag_key) != self.scope.tag_value:
            raise TargetVerificationError("Auto Scaling group does not have the required test tag")
        instances = [item for item in group.get("Instances", []) if item.get("InstanceId") == target_hint]
        if len(instances) != 1 or instances[0].get("LifecycleState") != "InService":
            raise TargetVerificationError("Instance is not an in-service member of the configured group")
        arn = group.get("AutoScalingGroupARN", "")
        expected_arn = f"arn:aws:autoscaling:{self.scope.region}:{self.scope.account_id}:autoScalingGroup:"
        if not arn.startswith(expected_arn):
            raise TargetVerificationError("Auto Scaling group ARN does not match the configured scope")
        return VerifiedTarget(
            account_id=self.scope.account_id, region=self.scope.region, instance_id=target_hint,
            group_name=self.scope.group_name, group_arn=arn,
            target_group_arns=group.get("TargetGroupARNs", []),
        )
