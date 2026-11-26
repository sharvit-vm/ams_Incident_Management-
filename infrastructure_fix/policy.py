"""Deterministic policy for considering a scale-out, independent of model output."""
from datetime import datetime

from infrastructure_fix.models import EvidenceRecord, VerifiedOnPremTarget, VerifiedTarget


def _aligned_recent_periods(cpu: list[dict], traffic: list[dict]) -> bool:
    """Compare corresponding recent five-minute buckets within 90 seconds."""
    try:
        for cpu_point, traffic_point in zip(cpu[:2], traffic[:2], strict=True):
            cpu_time = datetime.fromisoformat(cpu_point["timestamp"])
            traffic_time = datetime.fromisoformat(traffic_point["timestamp"])
            if cpu_time.tzinfo is None or traffic_time.tzinfo is None:
                return False
            if abs((cpu_time - traffic_time).total_seconds()) > 90:
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def assess_scale_out_candidate(
    evidence: list[EvidenceRecord], target: VerifiedTarget, application_process_names: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Allow consideration only; this never executes a change or approves one."""
    records = {item.kind: item for item in evidence}
    required = ("group", "target_health", "cpu", "traffic", "host_diagnostics")
    if any(name not in records or records[name].status != "available" for name in required):
        return False, "Required AWS evidence is missing, stale, or unavailable."
    if not application_process_names:
        return False, "Trusted application process names are not configured."
    processes = records["host_diagnostics"].details.get("processes", [])
    if not any(p.get("name") in application_process_names and p.get("cpu_percent", 0) >= 50 for p in processes):
        return False, "Host diagnostics do not show a configured application process consuming substantial CPU."
    group = records["group"].details
    desired, maximum = group.get("desired"), group.get("maximum")
    if not isinstance(desired, int) or not isinstance(maximum, int) or desired != 1 or maximum < 2:
        return False, "The test group is not eligible for the bounded 1-to-2 scenario."
    if group.get("enabled_scaling_policies") != 0:
        return False, "An enabled scaling policy may conflict with the proposed action."
    if any(status not in {"Successful", "Failed", "Cancelled"} for status in group.get("scaling_activity_statuses", [])):
        return False, "Scaling activity is already in progress or its state is unknown."
    targets = records["target_health"].details.get("targets", [])
    if not any(t.get("id") == target.instance_id and t.get("state") == "healthy" for t in targets):
        return False, "The affected target is not confirmed healthy."
    cpu = records["cpu"].details.get("points", [])
    traffic = records["traffic"].details.get("points", [])
    if len(cpu) < 2 or any(p["value"] < 80 for p in cpu[:2]) or len(traffic) < 4:
        return False, "CPU or traffic history does not establish the required sustained trend."
    for points, count in ((cpu, 2), (traffic, 4)):
        times = [datetime.fromisoformat(p["timestamp"]) for p in points[:count]]
        if any(not 180 <= (times[i] - times[i + 1]).total_seconds() <= 420 for i in range(len(times) - 1)):
            return False, "Metric periods are incomplete or not contiguous."
    if not _aligned_recent_periods(cpu, traffic):
        return False, "CPU and traffic observations do not cover the same recent periods."
    recent = sum(p["value"] for p in traffic[:2]) / 2
    earlier = sum(p["value"] for p in traffic[2:4]) / 2
    if earlier <= 0 or recent < earlier * 1.2:
        return False, "Request volume did not increase sufficiently relative to the observed baseline."
    return True, "CPU and request trends support considering a bounded scale-out; causation remains unproven."


def assess_rate_limit_candidate(
    evidence: list[EvidenceRecord], target: VerifiedOnPremTarget, application_process_names: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Evaluate the on-prem test case without changing the server."""
    records = {item.kind: item for item in evidence}
    if any(kind not in records or records[kind].status != "available" for kind in ("cpu", "traffic", "host_diagnostics", "rate_limit")):
        return False, "Required on-prem evidence is missing or unavailable."
    limiter = records["rate_limit"].details
    if limiter.get("prepared") is not True or limiter.get("enabled") is not False:
        return False, "The prepared rate limit is missing, already enabled, or unknown."
    if not application_process_names or not any(
        p.get("name") in application_process_names and p.get("cpu_percent", 0) >= 50
        for p in records["host_diagnostics"].details.get("processes", [])
    ):
        return False, "The configured application process is not confirmed to be consuming substantial CPU."
    cpu = records["cpu"].details.get("points", [])
    traffic = records["traffic"].details.get("points", [])
    if len(cpu) < 2 or any(p["value"] < 80 for p in cpu[:2]) or len(traffic) < 4:
        return False, "CPU or endpoint traffic history does not establish the required trend."
    for points, count in ((cpu, 2), (traffic, 4)):
        times = [datetime.fromisoformat(point["timestamp"]) for point in points[:count]]
        if any(not 180 <= (times[i] - times[i + 1]).total_seconds() <= 420 for i in range(len(times) - 1)):
            return False, "Metric periods are incomplete or not contiguous."
    if not _aligned_recent_periods(cpu, traffic):
        return False, "CPU and traffic observations do not cover the same recent periods."
    recent = sum(p["value"] for p in traffic[:2]) / 2
    earlier = sum(p["value"] for p in traffic[2:4]) / 2
    if earlier <= 0 or recent < earlier * 1.2:
        return False, "Endpoint requests did not increase sufficiently relative to the observed baseline."
    return True, "Endpoint load and CPU support considering the prepared rate limit; causation remains unproven."
