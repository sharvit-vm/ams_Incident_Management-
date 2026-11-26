"""LangChain read-only tools bound to a verified investigation target."""

from langchain_core.tools import tool

from infrastructure_fix.evidence_session import EvidenceSession


def build_investigation_tools(session: EvidenceSession, platform: str = "aws"):
    """Bind read-only tools to the verified target; collect data on tool invocation."""
    read = session.read

    @tool
    def get_group_state() -> dict:
        """Read the verified Auto Scaling group's capacity, policies and scaling activity."""
        return read("group")

    @tool
    def get_target_health() -> dict:
        """Read the load balancer target health for the verified group."""
        return read("target_health")

    @tool
    def get_cpu_and_credits() -> dict:
        """Read time-windowed EC2 CPU and CPU-credit measurements."""
        return read("cpu", "cpu_credits")

    @tool
    def get_traffic_and_latency() -> dict:
        """Read target request volume, p95 target response time and target 5xx counts."""
        return read("traffic", "latency", "target_5xx")

    @tool
    def get_process_and_service_summary() -> dict:
        """Read bounded host diagnostics or the reason they are unavailable."""
        return read("host_diagnostics")

    @tool
    def get_server_metrics() -> dict:
        """Read CPU and memory history for the verified on-premises test server."""
        return read("cpu", "memory")

    @tool
    def get_endpoint_metrics() -> dict:
        """Read request-volume history for the prepared on-prem test endpoint."""
        return read("traffic")

    @tool
    def get_rate_limit_state() -> dict:
        """Read whether the prepared on-prem endpoint rate limit exists and is enabled."""
        return read("rate_limit")

    @tool
    def get_relevant_logs() -> dict:
        """Read the bounded on-premises application error-count summary."""
        return read("logs")

    if platform == "aws":
        return [get_group_state, get_target_health, get_cpu_and_credits,
                get_traffic_and_latency, get_process_and_service_summary]
    if platform == "onprem":
        return [get_server_metrics, get_endpoint_metrics, get_process_and_service_summary, get_rate_limit_state, get_relevant_logs]
    raise ValueError("Unsupported infrastructure platform")
