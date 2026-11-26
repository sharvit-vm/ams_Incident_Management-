# Infrastructure investigation

The L2 infrastructure route invokes a read-only investigation. It is disabled by default and never changes a rate limit, scales an Auto Scaling group, or creates an approval. It accepts only `performance-simulation` incidents and requires a target hint that matches the configured AWS or on-premises scope. Configure the trusted test scope separately from incoming incident data:

```env
INFRA_INVESTIGATION_ENABLED=false
INFRA_PLATFORM=aws
INFRA_AWS_ACCOUNT_ID=
INFRA_AWS_REGION=
INFRA_ASG_NAME=
INFRA_ASG_TAG_KEY=
INFRA_ASG_TAG_VALUE=
# Optional: fixed, separately reviewed read-only diagnostics document
INFRA_SSM_DIAGNOSTIC_DOCUMENT_ARN=
INFRA_SSM_DIAGNOSTIC_DOCUMENT_VERSION=
# Comma-separated, trusted process names expected from the document
INFRA_APP_PROCESS_NAMES=
```

Set the flag to `true` only after configuring an explicitly authorized test group and read-only AWS identity. The backend can use an EC2 role or locally configured short-lived AWS credentials through the normal boto3 credential chain; do not place long-lived credentials in the repository. This is separate from the eventual remediation identity. Install `boto3` from the project requirements in the backend environment.

The read-only identity needs `sts:GetCallerIdentity`, `autoscaling:DescribeAutoScalingGroups`, `autoscaling:DescribeScalingActivities`, `autoscaling:DescribePolicies`, `elasticloadbalancing:DescribeTargetHealth`, `elasticloadbalancing:DescribeTargetGroups`, and `cloudwatch:GetMetricData`. Scope permissions where AWS supports it; describe and metric actions may require `Resource: *`. Protect test-group tags and configuration from this identity. The current implementation expects one group and exactly one attached application load-balancer target group.

## On-premises test adapter

Set `INFRA_PLATFORM=onprem` and configure `INFRA_ONPREM_TARGET_ID`, `INFRA_ONPREM_HOST`, `INFRA_ONPREM_SSH_USER`, `INFRA_ONPREM_KNOWN_HOSTS`, `INFRA_ONPREM_ENDPOINT`, and `INFRA_APP_PROCESS_NAMES`. The incident's target hint must exactly match the trusted target ID. The configured SSH account must be restricted, use key-based authentication and a verified host key, and be allowed to run only the administrator-installed read-only `/usr/local/sbin/ams-infra-diagnostics --json` command. The adapter does not accept a host or command from the incident or model.

The diagnostic executable is **not yet provided or deployed**. Its bounded JSON output must contain `schema_version: 1`, the matching `target_id`, an ISO 8601 timezone-aware `observed_at`, and objects `cpu`, `memory`, `traffic`, `host_diagnostics`, `rate_limit`, and `logs`. CPU, memory and traffic each contain `points`, an array of up to 12 `{timestamp, value}` entries at five-minute intervals; CPU and memory are 0–100 host percentages and traffic is request count per period for the prepared endpoint. Both `traffic` and `rate_limit` must include an `endpoint` equal to the trusted configured path. `host_diagnostics` contains `processes` (`name`, `cpu_percent`) and `services` (`name`, `status`); `rate_limit` contains boolean `prepared` and `enabled`; `logs` contains a non-negative `error_count` and `window_minutes` from 1 to 60. Missing optional evidence is explicitly unavailable; no raw log text or command lines are exposed. Stale or invalid evidence is marked unavailable. Until the executable and its monitoring inputs are implemented on the test host, this adapter cannot conduct a real investigation.

For on-premises investigations the LangChain tools are `get_server_metrics`, `get_endpoint_metrics`, `get_process_and_service_summary`, `get_rate_limit_state`, and `get_relevant_logs`. The first tool call executes the fixed diagnostic for the verified host; subsequent calls use the same bounded result. No agent tool can enable the limiter. A prepared but disabled limiter and evidence that endpoint load and a configured application process are driving CPU are prerequisites for a rate-limit candidate. Human approval and the actual limiter executor are separate future work.

Optional host diagnostics require `ssm:SendCommand` restricted to the exact reviewed custom document and designated instance, plus `ssm:GetCommandInvocation`. Configure an in-account, in-region document ARN and a numeric pinned version; the model and incident cannot supply commands or document parameters. The document must be provisioned and independently reviewed before use, must itself be read-only, and must emit a JSON object such as `{"schema_version":1,"processes":[{"name":"python","cpu_percent":90.0}],"services":[{"name":"app.service","status":"active"}]}`. At most 10 processes and 5 services are accepted; command lines and unexpected fields are discarded. This repository does not provision or validate the document's underlying code, so IAM policy and document ownership are essential. Unconfigured, malformed, failed or timed-out diagnostics are recorded as unavailable.

The collector queries five-minute CloudWatch periods over 30 minutes for EC2 CPU, CPU credit balance, target-group request count per target, ALB p95 target response time and target 5xx counts; it also reads group state, scaling policies, recent activities and target health. Missing, stale, partial or failed telemetry is reported as unavailable. CPU credits may not exist for non-burstable instances; a missing 5xx metric is not assumed to be zero. ALB p95 measures target response time to response headers, not end-to-end latency.

The model receives separate read-only tools for group state, target health, CPU and credits, traffic and latency, and optional host diagnostics, plus untrusted incident context. The application verifies the target before the model runs; the model cannot choose a different AWS target. Each tool fetches the relevant AWS evidence on its first invocation; subsequent calls reuse that result for the investigation. Structured output must cite available collected evidence. A separate code gate marks a scale-out as a **candidate only** when the group and target are eligible, CPU is high in two contiguous periods, requests increased by at least 20% relative to the prior two periods, and an explicitly configured application process consumes at least 50% CPU in host diagnostics. Without host evidence or configured process names the candidate is blocked. These are preliminary demo thresholds, not proof of causality or traffic legitimacy. Host diagnostics require a separately deployed document; a calculated error rate, a real approval plan, execution, and post-action verification remain to be implemented. No scaling action is available to this agent.

Langfuse uses the existing `trace_span` and `generation_span` wrappers and the workflow session ID; token use uses the existing LangChain callback collector. The model receives a structured prompt and read-only evidence tools. Observability failures must not change the investigation outcome.

## Module responsibilities

- `agent.py`: orchestrates verification, collection, model invocation, tracing and the final result.
- `target_resolver.py` and `onprem.py`: verify an incident target and collect evidence for the respective trusted environment.
- `evidence.py` and `host_diagnostics.py`: collect bounded AWS evidence; host diagnostics uses only the optional fixed SSM document.
- `tools.py` and `evidence_session.py`: define the LangChain tools, bind them to the verified target, collect and cache evidence on invocation, and expose no AWS write operations.
- `investigation.py`: constructs the LangChain agent from the model, tools, prompt and structured response.
- `prompts.py`, `models.py` and `policy.py`: hold instructions, data contracts and the deterministic candidate policy respectively.
