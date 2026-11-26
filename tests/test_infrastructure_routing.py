from unittest.mock import Mock, patch

from issuelayer.intake.schemas import ErrorEvent
from issuelayer.intake.source_event import SourceEvent
from l2_rca.agents.l2_rca_agent import L2RCAAgent
from l2_rca.models.diagnosis import Diagnosis
from l2_rca.models.request import IncidentRequest
from workflows.intake_categorisation_workflow import build_intake_categorisation_workflow


def _diagnosis(domain):
    return Diagnosis(
        problem_domain=domain,
        recommended_agent="infrastructure_fix_agent" if domain == "Infrastructure" else "db_fix_agent",
        confidence=0.9,
        reason="Test diagnosis",
        recommended_checks=["Inspect the target"],
    )


def _run_graph(domain):
    event = ErrorEvent(
        id="event-1", fingerprint="test", error_type="HighCPU", message="High CPU",
        incident_id="INC001", configuration_item="i-0123456789abcdef0",
        impacted_environment="performance-simulation", description="Test incident",
    )
    source = SourceEvent(source="servicenow", event_type="incident", external_id="INC001")
    fake_db = Mock(return_value={"status": "WAITING_FOR_APPROVAL"})
    real_agent = L2RCAAgent()
    real_agent.diagnosis_service.diagnose = Mock(return_value=(_diagnosis(domain), 1, 0))
    real_agent.agent_executor.execute = Mock(side_effect=AssertionError("L2 dispatched twice"))
    decision = Mock(allowed=True, risk_level="low", reason="ok")
    decision.to_dict.return_value = {"allowed": True}

    def lookup(name):
        from workflows.agent_registry import get_fix_agent
        return fake_db if name.lower() == "database" else get_fix_agent(name)

    with patch("observability.agent_trace._get_langfuse_client", return_value=None), \
         patch("workflows.intake_categorisation_workflow.normalise_source_event", return_value=event), \
         patch("workflows.intake_categorisation_workflow.validate_guardrails", return_value=decision), \
         patch("workflows.intake_categorisation_workflow.categorise_error_event", return_value={"support_level": "L2", "technology": domain}), \
         patch("workflows.intake_categorisation_workflow.L2RCAAgent", return_value=real_agent), \
         patch("workflows.intake_categorisation_workflow.get_fix_agent", side_effect=lookup), \
         patch("workflows.intake_categorisation_workflow._record_connector_dashboard_update"):
        result = build_intake_categorisation_workflow().invoke({
            "source": "servicenow", "source_event": source, "payload": {}, "headers": {},
        })
    real_agent.agent_executor.execute.assert_not_called()
    return result, fake_db


def test_infrastructure_diagnosis_stops_when_investigation_disabled():
    result, fake_db = _run_graph("Infrastructure")
    assert result["l2_rca_result"]["status"] == "completed"
    assert result["l2_rca_result"]["data"]["execution"]["status"] == "DEFERRED_TO_WORKFLOW"
    assert result["fix_agent_result"]["status"] == "INVESTIGATION_DISABLED"
    fake_db.assert_not_called()


def test_database_fix_is_dispatched_once_by_workflow():
    result, fake_db = _run_graph("Database")
    assert result["fix_agent_result"]["status"] == "WAITING_FOR_APPROVAL"
    fake_db.assert_called_once()
    assert fake_db.call_args.args[0].ticket_id == "INC001"


def test_standalone_l2_keeps_existing_downstream_dispatch():
    agent = L2RCAAgent()
    agent.diagnosis_service.diagnose = Mock(return_value=(_diagnosis("Database"), 1, 0))
    agent.agent_executor.execute = Mock(return_value={"status": "sent"})
    request = IncidentRequest(ticket_id="INC001", classification="INCIDENT", support_level="L2", technology="Database", team="DB", application="app", title="DB issue", description="test", priority="P2", confidence=0.9)
    result = agent.analyze(request)
    agent.agent_executor.execute.assert_called_once()
    assert result.data["execution"]["status"] == "sent"
