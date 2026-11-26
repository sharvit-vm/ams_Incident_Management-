"""Trusted on-prem target and read-only fixed-command evidence adapter."""
import json
import math
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from infrastructure_fix.evidence import _record
from infrastructure_fix.models import VerifiedOnPremTarget
from infrastructure_fix.target_resolver import TargetVerificationError

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SAFE_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}\Z")
_DIAGNOSTIC_COMMAND = "/usr/local/sbin/ams-infra-diagnostics --json"
_KINDS = ("cpu", "memory", "traffic", "host_diagnostics", "rate_limit", "logs")
_MAX_OUTPUT_BYTES = 32000


def _run_bounded(command, *, timeout_seconds):
    """Read at most the configured limit from the diagnostic's stdout."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = bytearray()
    errors = []

    def read_output():
        try:
            output.extend(process.stdout.read(_MAX_OUTPUT_BYTES + 1))
            if len(output) > _MAX_OUTPUT_BYTES:
                process.kill()
        except Exception as exc:
            errors.append(exc)
            process.kill()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        reader.join(timeout=timeout_seconds)
        if reader.is_alive():
            raise TimeoutError("diagnostic output reader did not finish")
        if errors or len(output) > _MAX_OUTPUT_BYTES or return_code != 0:
            raise ValueError("diagnostic command failed or output exceeded limit")
        return bytes(output).decode("utf-8")
    finally:
        process.stdout.close()


@dataclass(frozen=True)
class OnPremScope:
    target_id: str
    host: str
    ssh_user: str
    known_hosts_file: str
    endpoint: str

    def __post_init__(self):
        if not _SAFE_ID.fullmatch(self.target_id) or not _SAFE_ID.fullmatch(self.host):
            raise TargetVerificationError("Trusted on-prem target and host must be configured as safe identifiers")
        if not _SAFE_USER.fullmatch(self.ssh_user):
            raise TargetVerificationError("A trusted restricted SSH user is required")
        if not self.known_hosts_file or not Path(self.known_hosts_file).is_file():
            raise TargetVerificationError("An existing trusted SSH known-hosts file is required")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,127}", self.endpoint) or ".." in self.endpoint:
            raise TargetVerificationError("A trusted test endpoint path is required")


class OnPremTargetResolver:
    def __init__(self, scope: OnPremScope):
        self.scope = scope

    def resolve(self, target_hint: str | None) -> VerifiedOnPremTarget:
        if not target_hint or target_hint != self.scope.target_id:
            raise TargetVerificationError("Incident target does not match the configured on-prem test server")
        return VerifiedOnPremTarget(target_id=self.scope.target_id, host=self.scope.host,
                                    ssh_user=self.scope.ssh_user, known_hosts_file=self.scope.known_hosts_file,
                                    endpoint=self.scope.endpoint)


class OnPremEvidenceCollector:
    """Run one operator-installed read-only diagnostic executable over authenticated SSH.

    The executable and its deployment are outside this repository; its JSON contract
    is documented in README.md. Incident and model text never form command arguments.
    """
    def __init__(self, *, runner=_run_bounded, timeout_seconds: int = 15):
        self.runner = runner
        self.timeout_seconds = min(max(timeout_seconds, 1), 30)

    def collect(self, target: VerifiedOnPremTarget):
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "IdentitiesOnly=yes", "-o", f"UserKnownHostsFile={target.known_hosts_file}",
                   "--", f"{target.ssh_user}@{target.host}", _DIAGNOSTIC_COMMAND]
        try:
            output = self.runner(command, timeout_seconds=self.timeout_seconds)
            payload = json.loads(output)
            return self._parse(payload, target)
        except Exception as exc:
            return [_record(kind, "onprem_diagnostics", "unavailable", {"error_type": type(exc).__name__}) for kind in _KINDS]

    def _parse(self, payload, target):
        if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("target_id") != target.target_id:
            raise ValueError("invalid diagnostic identity or schema")
        observed = datetime.fromisoformat(payload["observed_at"])
        now = datetime.now(timezone.utc)
        if observed.tzinfo is None or not now - timedelta(minutes=2) <= observed <= now + timedelta(seconds=30):
            raise ValueError("diagnostic result is stale or has an invalid timestamp")
        records = []
        for kind in _KINDS:
            raw = payload.get(kind)
            if not isinstance(raw, dict):
                records.append(_record(kind, "onprem_diagnostics", "unavailable", {"reason": "diagnostic data missing"}))
                continue
            try:
                if kind in {"traffic", "rate_limit"} and raw.get("endpoint") != target.endpoint:
                    raise ValueError("diagnostic endpoint does not match the trusted test endpoint")
                details = self._validate(kind, raw, now)
                records.append(_record(kind, "onprem_diagnostics", "available", details))
            except (ValueError, TypeError, KeyError):
                records.append(_record(kind, "onprem_diagnostics", "unavailable", {"reason": "invalid or stale diagnostic data"}))
        return records

    @staticmethod
    def _validate(kind, raw, now):
        if kind in {"cpu", "memory", "traffic"}:
            points = raw["points"]
            if not isinstance(points, list) or not 1 <= len(points) <= 12:
                raise ValueError("invalid metric count")
            clean = []
            for point in points:
                timestamp = datetime.fromisoformat(point["timestamp"])
                value = point["value"]
                if timestamp.tzinfo is None or not now - timedelta(minutes=30) <= timestamp <= now + timedelta(seconds=30):
                    raise ValueError("invalid metric timestamp")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError("invalid metric value")
                if kind in {"cpu", "memory"} and value > 100:
                    raise ValueError("invalid resource percentage")
                clean.append({"timestamp": timestamp.astimezone(timezone.utc).isoformat(), "value": value})
            clean.sort(key=lambda p: p["timestamp"], reverse=True)
            if datetime.fromisoformat(clean[0]["timestamp"]) < now - timedelta(minutes=10):
                raise ValueError("stale metric")
            metric = {"cpu": "cpu_percent", "memory": "memory_percent", "traffic": "endpoint_requests_per_period"}[kind]
            return {"points": clean, "metric": metric, "period_seconds": 300}
        if kind == "host_diagnostics":
            processes, services = raw["processes"], raw["services"]
            if not isinstance(processes, list) or len(processes) > 10 or not isinstance(services, list) or len(services) > 5:
                raise ValueError("invalid host diagnostics")
            clean_processes, clean_services = [], []
            for p in processes:
                name, cpu = p["name"], p["cpu_percent"]
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not math.isfinite(cpu) or not 0 <= cpu <= 102400:
                    raise ValueError("invalid process")
                clean_processes.append({"name": name, "cpu_percent": cpu})
            for service in services:
                name, status = service["name"], service["status"]
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", name) or status not in {"active", "inactive", "failed", "unknown"}:
                    raise ValueError("invalid service")
                clean_services.append({"name": name, "status": status})
            return {"processes": clean_processes, "services": clean_services}
        if kind == "logs":
            count, minutes = raw["error_count"], raw["window_minutes"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0 or isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 60:
                raise ValueError("invalid log summary")
            return {"error_count": count, "window_minutes": minutes}
        prepared, enabled = raw["prepared"], raw["enabled"]
        if not isinstance(prepared, bool) or not isinstance(enabled, bool):
            raise ValueError("invalid rate limiter state")
        return {"prepared": prepared, "enabled": enabled}
