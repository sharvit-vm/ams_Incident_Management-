"""Evidence-bound LangChain analysis; AWS operations remain outside the model."""
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy

from infrastructure_fix.models import InvestigationResult
from infrastructure_fix.prompts import SYSTEM_PROMPT
from infrastructure_fix.tools import build_investigation_tools


def build_investigation_agent(model, evidence_session, platform="aws"):
    return create_agent(
        model=model,
        tools=build_investigation_tools(evidence_session, platform),
        system_prompt=SYSTEM_PROMPT,
        response_format=ToolStrategy(InvestigationResult),
    )
