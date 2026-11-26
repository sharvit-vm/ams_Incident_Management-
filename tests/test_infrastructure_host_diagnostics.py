import json
from unittest.mock import Mock

from infrastructure_fix.host_diagnostics import HostDiagnostics
from infrastructure_fix.models import VerifiedTarget


def _target():
    return VerifiedTarget(account_id="123456789012", region="us-east-1", instance_id="i-0123456789abcdef0", group_name="demo", group_arn="arn")


def test_fixed_document_returns_only_allowed_fields():
    ssm = Mock()
    ssm.send_command.return_value = {"Command": {"CommandId": "command-1"}}
    ssm.get_command_invocation.return_value = {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": json.dumps({"schema_version": 1, "processes": [{"name": "python", "cpu_percent": 91, "command": "do not expose"}], "services": [{"name": "demo.service", "status": "active"}]})}
    result = HostDiagnostics(ssm, "arn:aws:ssm:us-east-1:123456789012:document/diag", "3").collect(_target())
    assert result.status == "available"
    assert result.details["processes"] == [{"name": "python", "cpu_percent": 91}]
    call = ssm.send_command.call_args.kwargs
    assert call["DocumentVersion"] == "3" and call["InstanceIds"] == [_target().instance_id]
    assert "Parameters" not in call


def test_untrusted_document_and_unpinned_version_do_not_execute():
    ssm = Mock()
    result = HostDiagnostics(ssm, "arn:aws:ssm:us-east-1:999999999999:document/diag", "$LATEST").collect(_target())
    assert result.status == "unavailable"
    ssm.send_command.assert_not_called()


def test_invalid_output_and_failed_command_return_unavailable():
    ssm = Mock()
    ssm.send_command.return_value = {"Command": {"CommandId": "command-1"}}
    ssm.get_command_invocation.return_value = {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": '{"schema_version": 1, "processes": [{"name":"$(bad)", "cpu_percent":5}], "services": []}'}
    agent = HostDiagnostics(ssm, "arn:aws:ssm:us-east-1:123456789012:document/diag", "1")
    assert agent.collect(_target()).status == "unavailable"
    ssm.get_command_invocation.return_value = {"Status": "Failed"}
    assert agent.collect(_target()).status == "unavailable"
