"""Optional fixed-document host diagnostics; never accepts commands from an incident or model."""
import json
import math
import re
import time

from infrastructure_fix.evidence import _record


class HostDiagnostics:
    def __init__(self, ssm, document_arn: str, document_version: str, *, timeout_seconds: int = 15):
        self.ssm = ssm
        self.document_arn = document_arn
        self.document_version = document_version
        self.timeout_seconds = min(max(timeout_seconds, 1), 30)

    def collect(self, target):
        prefix = f"arn:aws:ssm:{target.region}:{target.account_id}:document/"
        if not self.document_arn.startswith(prefix) or not re.fullmatch(r"[1-9][0-9]*", self.document_version):
            return _record("host_diagnostics", "ssm", "unavailable", {"reason": "trusted document ARN and immutable numeric version required"})
        try:
            response = self.ssm.send_command(
                InstanceIds=[target.instance_id], DocumentName=self.document_arn,
                DocumentVersion=self.document_version, TimeoutSeconds=self.timeout_seconds,
                MaxConcurrency="1", MaxErrors="0",
            )
            command_id = response["Command"]["CommandId"]
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                try:
                    invocation = self.ssm.get_command_invocation(CommandId=command_id, InstanceId=target.instance_id)
                except Exception as exc:
                    if getattr(exc, "response", {}).get("Error", {}).get("Code") != "InvocationDoesNotExist":
                        raise
                    time.sleep(1)
                    continue
                state = invocation.get("Status")
                if state == "Success" and invocation.get("ResponseCode") == 0:
                    output = invocation.get("StandardOutputContent", "")
                    if not isinstance(output, str) or len(output) > 16000:
                        return _record("host_diagnostics", "ssm", "unavailable", {"reason": "diagnostic output exceeds limit"})
                    return self._parse(output)
                if state in {"Failed", "TimedOut", "Cancelled", "Cancelling"}:
                    return _record("host_diagnostics", "ssm", "unavailable", {"reason": "diagnostic command did not succeed", "command_status": state})
                time.sleep(1)
            return _record("host_diagnostics", "ssm", "unavailable", {"reason": "diagnostic command timed out"})
        except Exception as exc:
            return _record("host_diagnostics", "ssm", "unavailable", {"error_type": type(exc).__name__})

    def _parse(self, output):
        try:
            payload = json.loads(output)
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise ValueError("invalid schema")
            processes = payload.get("processes")
            services = payload.get("services")
            if not isinstance(processes, list) or len(processes) > 10 or not isinstance(services, list) or len(services) > 5:
                raise ValueError("invalid list")
            clean_processes = []
            for process in processes:
                name, cpu = process.get("name"), process.get("cpu_percent")
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not math.isfinite(cpu) or cpu < 0 or cpu > 102400:
                    raise ValueError("invalid process")
                clean_processes.append({"name": name, "cpu_percent": cpu})
            clean_services = []
            for service in services:
                name, status = service.get("name"), service.get("status")
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", name) or status not in {"active", "inactive", "failed", "unknown"}:
                    raise ValueError("invalid service")
                clean_services.append({"name": name, "status": status})
            return _record("host_diagnostics", "ssm", "available", {"processes": clean_processes, "services": clean_services})
        except (ValueError, TypeError, AttributeError):
            return _record("host_diagnostics", "ssm", "unavailable", {"reason": "diagnostic output did not match the allowed schema"})
