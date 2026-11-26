# EC2 performance incident: investigation and horizontal scaling

## Goal and resolution

Demonstrate a controlled performance incident: Locust generates load against a test application, New Relic alerts on sustained high CPU, and AMS investigates. When evidence supports a capacity shortage, an infrastructure agent proposes increasing the **designated test Auto Scaling group's desired capacity from 1 to 2**. After human approval, a bounded executor requests the increase and checks whether a new instance becomes healthy and performance improves.

The extra instance must run the same application and receive traffic through an Application Load Balancer (ALB). Scaling is a proposed mitigation, not proof of a root-cause fix. A code defect, uneven traffic, a downstream bottleneck, or insufficient evidence may need another response. Nginx is not required for the AWS demo.

For a second, on-premises scenario without horizontal scaling, the agent verifies a designated test server and investigates its CPU, application process, service health, endpoint traffic and prepared rate-limiter state. If the evidence supports endpoint overload, it may propose enabling an existing endpoint-specific rate limit after human approval; the report must include the impact of rejected or delayed requests. The rate limiter and the separately approved executor must be provided by the chosen environment. A restart is not a default response to sustained request load.

Both scenarios share the LangChain investigator, evidence models, human approval and verification concepts. Trusted configuration chooses the environment and scopes targets; incident text cannot choose a host, command, AWS account, or execution privilege. AWS scale-out and on-premises rate limiting require separate environment-specific adapters and executors. No action is performed by investigation.

```text
Locust on separate machine -> ALB -> EC2 application in test Auto Scaling group
                                      -> New Relic detects high CPU
                                      -> connector -> ServiceNow -> AMS
                                      -> L2 RCA -> infrastructure investigation
                                      -> proposed 1-to-2 scale-out -> human approval
                                      -> approved executor -> verify health and metrics
```

## What is complete

- [x] Connector performance webhook: shared token, simulation restriction, SQLite alert-ID tracking and scheduler switch.
- [x] Local Postman test created ServiceNow incident `INC0010185`; repeating the alert did not create another ticket. The user confirmed ServiceNow forwarded it to AMS and L2 RCA completed.
- [x] Connector committed and pushed: repository `vm-ec/ams-monitoring-connector`, branch `feature/newrelic-performance-integration`, commit `d55fbc1`.
- [x] AMS Phase 1 routing implemented locally on `feature/infrastructure-remediation-routing`: LangGraph dispatches after L2, infrastructure reaches a non-executing placeholder, and simulated checks cover database and standalone L2 routing.
- [x] AMS Phase 1 changes and the initial plan were committed and pushed as `3b589ad`. Subsequent Phase 2 work remains local and uncommitted.
- [ ] The pending human-review card for `INC0010185` has not been diagnosed; inspect its execution and approval ID when the running environment is available.
- [ ] A real New Relic alert, infrastructure investigation, approval, AWS execution, and verification have not yet been tested.

## How the test environment works

1. Locust runs separately and sends requests to the ALB DNS name. The application exposes `/health` and a controlled CPU-bound `/cpu-heavy` endpoint.
2. An EC2 Auto Scaling group starts at desired capacity 1, with a launch template that starts the same application on every instance. Its target group uses `/health` to check readiness.
3. Locust is ramped gradually until CPU and latency cross agreed alert thresholds. User count does not guarantee a specific CPU level.
4. New Relic sends an alert to the connector; the existing ServiceNow Business Rule forwards the incident to AMS.
5. The infrastructure agent verifies the affected instance, group, account, region, and target group before drawing conclusions.
6. If the evidence supports capacity pressure, it proposes 1 -> 2. After approval, it checks the state again and requests that change. It then waits for instance launch, registration, health, warm-up, and a fresh observation window.

For this demonstration, keep any independent automatic scaling policies from competing with the agent's approval flow; choose and document the test group configuration explicitly. Monitor instance, ALB, telemetry, and burst-credit costs, and define a stop time and cleanup owner.

## Phase 1: AMS routing — complete locally

- [x] Move downstream dispatch for the LangGraph path out of L2 and into the graph, preserving standalone L2 behavior.
- [x] Register an infrastructure placeholder and define a request carrying incident, diagnosis, environment, and an **unverified** target hint.
- [x] Verify with simulated inputs that infrastructure reaches the placeholder and database dispatch occurs once.
- [x] Committed and pushed AMS Phase 1 on the feature branch. Real DB and AWS integrations were not part of these checks.

## Phase 2: Infrastructure investigation agent

**Goal:** Identify the correct test workload and collect evidence before recommending any action.

### Data contracts

- [ ] Define a verified target with AWS account ID, region, instance ID, Auto Scaling group ARN/name, target group ARN, environment, and verification source.
- [ ] Define evidence records with value, source, observation window, timestamp, target, and success or error status.
- [x] Start verified-target and evidence models and a read-only target resolver; test account, region, group, tag and membership checks with fake clients. The evidence model and target resolution still need to be expanded for full investigation.
- [x] Connect a read-only LangChain investigation agent to the workflow with fail-closed target verification, AWS evidence collection and Langfuse trace spans. Investigation is disabled by default and must be configured for a designated test group.
- [x] Add group state, target health, CPU, CPU-credit, traffic, target 5xx counts and p95 response-time evidence; implement a conservative candidate gate and test with fake clients and model output.
- [ ] Carry a reliable target reference from the New Relic alert through ServiceNow and AMS. ServiceNow `cmdb_ci` is a reference field; test the actual Business Rule payload and use a trusted mapping or explicit field if necessary.
- [ ] Stop for human investigation if the target is absent, ambiguous, outside the allowlist, wrongly tagged, stale, or cannot be verified. Incident text and LLM output are untrusted hints.

### Investigation tools

| Tool | Data source and purpose |
| --- | --- |
| `resolve_target` | EC2, Auto Scaling and trusted configuration: resolve instance to the designated account, region, test group and tags. |
| `get_group_state` | Auto Scaling: desired/min/max, in-service instances, launch status, scaling activities and applicable policies. |
| `get_target_health` | ALB: attached target group, registered instance health and health-check failures. |
| `get_cpu_and_credits` | CloudWatch: time-windowed EC2 CPU and burst-credit metrics, including missing/stale data. |
| `get_traffic_and_latency` | Configured New Relic or ALB/application metrics: request rate, latency, error rate and their time windows. Define and verify the actual metric names during setup. |
| `get_process_and_service_summary` | Optional Systems Manager diagnostic document: bounded CPU/process and service-health summaries if needed to distinguish application load from another process. |

- [x] Implement structured, bounded read-only adapters with timeouts and explicit errors. A trusted decision layer enforces tool scope; no arbitrary LLM-generated AWS calls or shell commands.
- [x] Implement an optional fixed-document SSM diagnostic adapter with a pinned document version, bounded runtime and sanitized output. The reviewed read-only AWS document and scoped IAM permissions still need to be provisioned and tested.
- [ ] Correlate high CPU, request load, application/process data, latency and group/target health. Do not infer that scaling will help from CPU alone.
- [ ] Propose scaling only when evidence supports capacity pressure, the group is eligible and capacity limits permit it; otherwise report uncertainty or request human investigation.
- [ ] Extend fake-client tests to cover unrelated CPU consumers, full groups and all other operational cases; verify real AWS dimensions, IAM and telemetry on the designated test environment.

**Current state:** The agent produces a traceable investigation and a preliminary scale-out candidate flag, or explains why investigation cannot proceed. It does not create a proposal or change AWS resources. Process-level evidence requires a separately provisioned fixed SSM document; a calculated error rate and live AWS validation remain outstanding.

## Phase 3: Human approval, controlled execution, verification

- [ ] Create a `RemediationPlan` using the existing approval store with incident, evidence, verified group/region, current and proposed desired capacities, expected impact, cost implication, risks, expiry, and verification/cleanup approach.
- [ ] Make the actual plan and approval ID visible in the dashboard. Investigate the distinction between categorization review and remediation approval.
- [ ] Register an infrastructure executor with permissions limited to `autoscaling:SetDesiredCapacity` for the designated test group. The agent itself remains read-only.
- [ ] On approval, re-read identity, tags, current capacity, min/max, target health, scaling activities and policy state. Stop if the approval is stale, capacity changed, the group is already scaling, a policy conflicts, or the expected 1 -> 2 transition is no longer valid.
- [ ] Bound the change to an approved increment of one and the configured maximum. Make approval consumption and execution idempotent; handle timeouts and uncertain AWS responses by re-reading actual state, never blindly repeating an increase.
- [ ] Record approval, requested change, AWS response/activity, correlation ID, and timestamps. Wait for launch, ALB registration, healthy status and warm-up using bounded polling and explicit timeout/failure outcomes.
- [ ] Compare fresh before/after CPU, request volume, latency, errors and target health while Locust remains running. Report improved, not improved, or verification incomplete; never declare success just because desired capacity changed.
- [ ] Define cleanup separately: after the demo, an authorized operator reviews whether returning desired capacity to 1 is safe. Scale-in may terminate an instance; it is not an automatic rollback.
- [ ] Test the complete approval/executor lifecycle with fake AWS clients, including denial, expiry, races, duplicate approval, conflicts, launch failure, unhealthy targets, timeout, and partial or inconclusive results.
- [ ] Add an on-premises rate-limit approval proposal and a separately scoped executor that enables only a prepared limiter after approval; verify health and request rejection/delay and support operator-reviewed rollback.

**Completion:** An approved simulated proposal changes the fake group once and produces an evidence-based result; rejected or unsafe proposals cannot change it.

## Phase 4: Build the test environment

- [ ] Choose AWS account, region, owner, budget, lifetime, test resource tags and target allowlist.
- [ ] Build the small stateless test app with `/health` and `/cpu-heavy`; ensure it can run identically on multiple instances.
- [ ] Create a launch template, an Auto Scaling group initially at desired 1 with maximum at least 2, an ALB, target group and health check. Restrict network access to the intended test clients.
- [ ] Ensure each instance starts the app and receives the required monitoring configuration; verify a manually launched test instance can become healthy before the demo.
- [ ] Configure instance IAM and Systems Manager Agent/connectivity if host diagnostics are used. Use the AMS backend's IAM role or short-lived local credentials for AWS access, with separate investigation and remediation permissions.
- [ ] Configure CloudWatch and New Relic visibility for EC2 CPU, relevant traffic/latency/errors and application logs. Confirm metric dimensions and observation intervals.
- [ ] Run Locust separately and measure a controlled baseline and ramp. Record CPU credit mode/balance for burstable instances and agree thresholds and a stop condition.

**Completion:** One healthy application instance serves load through the ALB; metrics show a controlled increase, and the launch template can produce a second healthy instance.

## On-premises development and test track

- [x] Add a trusted on-prem target scope, fixed-command SSH evidence adapter, environment-specific LangChain tools, and a candidate policy. The incident and model cannot supply commands or select an unconfigured server.
- [ ] Implement, review and provision the on-host read-only diagnostic executable and its metrics sources; configure a restricted SSH identity and verified host key. Its versioned output contract is documented in `infrastructure_fix/README.md`.
- [ ] Implement a prepared endpoint-specific limiter in the selected on-prem test environment, and expose its state through the diagnostic contract.
- [ ] Add the approved rate-limit executor, review/rollback workflow, and verification of CPU, latency, health and HTTP 429/delayed requests while Locust continues.
- [ ] Validate the end-to-end path against a designated on-prem test host. Never infer that a diagnostic adapter is operational from fake-client tests.

## Phase 5: Connect a real New Relic alert

- [ ] Fix connector handling of uncertain ServiceNow outcomes: reconcile before retrying when a response is interrupted. The current `FAILED` retry can duplicate an incident in that case.
- [ ] Choose durable alert storage appropriate to the deployment and concurrency; local SQLite is a single-host demo store.
- [ ] Deploy the connector with the committed branch and a public URL reachable by New Relic; keep token and other credentials out of Git.
- [ ] Configure the alert condition and workflow; verify the actual New Relic issue ID, state and payload map to the endpoint, including simulation environment and `X-Connector-Token`.
- [ ] Verify the ServiceNow Business Rule passes the target context AMS needs and that repeated notifications do not create multiple tickets.

**Completion:** A genuine New Relic alert creates one incident and carries enough trustworthy context for AMS to resolve the target.

## Phase 6: End-to-end demonstration

- [ ] Confirm desired capacity 1, a healthy ALB target, healthy application, monitoring and no conflicting test scaling policy.
- [ ] Start Locust on a separate machine and ramp gradually within the agreed time and cost limits.
- [ ] Confirm sustained high CPU and latency, the New Relic alert, one ServiceNow incident, AMS categorization and L2 RCA.
- [ ] Inspect agent evidence and the 1 -> 2 proposal; approve only if it matches the intended test group.
- [ ] Keep Locust running, apply the approved scale-out and confirm the new target becomes healthy and serves traffic.
- [ ] Compare before/after CPU, throughput, latency and error rate across the workload. Record the result and whether mitigation helped.
- [ ] Stop load and monitoring test activity. Have the designated operator review safe scale-in and resource cleanup.

## Tools and services needed

| Tool/service | Role in development and demo |
| --- | --- |
| Python, FastAPI, LangGraph | Existing AMS workflow and infrastructure agent integration. |
| AWS SDK for Python (boto3) | Implement EC2, Auto Scaling, CloudWatch, ALB and optional Systems Manager adapters; add and pin a compatible dependency if absent. |
| AWS IAM / STS | Role-based or short-lived credentials; separate investigation and bounded remediation access. |
| EC2 Auto Scaling and launch template | Maintain the test application's instances and apply the desired-capacity change. |
| Application Load Balancer and target group | Route requests and report target health. |
| CloudWatch | Time-windowed EC2 CPU and burst-credit metrics; other metrics depend on configuration. |
| AWS Systems Manager | Optional fixed-document host diagnostics and instance management; not required to call SetDesiredCapacity. |
| New Relic | Observe the workload and send a real alert to the connector. |
| ServiceNow and existing Business Rule | Create the incident and forward it to AMS. |
| Locust | Generate controlled HTTP load from a separate machine. |
| Existing approval store and dashboard | Present a concrete proposal and record human approval and execution result. |
| Fake AWS clients / Python tests | Exercise decisions, failures and approval behavior locally without changing AWS. |
| Langfuse | Observe workflow and agent steps when configured; it is not the workflow engine. |

## Immediate next action

Complete investigation coverage and validate the AWS integration in the designated test environment, then build the approval proposal and separate executor. The separate `ams-incident-agent` and `applications` repositories support the log-error flow and are not required for this direct performance-alert demonstration.
