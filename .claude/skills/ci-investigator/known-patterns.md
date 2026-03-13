# Known Patterns

Stable domain patterns discovered during CI investigations. This file grows over time as new patterns are confirmed across multiple builds or PRs.

## Failure Signatures

- **Short duration + failure** (build < 5 min, no test steps ran): infrastructure or provisioning failure -- the build never reached test execution.
- **Same step fails across many PRs**: systemic platform issue, not PR-specific. Check for platform-wide incidents.
- **"gather" step failures are secondary**: the gather step collects artifacts after a failure. Always look for an earlier failing step as the true root cause.
- **Step names containing "ipi-install" or "provision"**: cluster provisioning. Failures here indicate cloud provider issues, quota exhaustion, or region availability problems.
- **Lease step failures**: resource quota or pool exhaustion. Check lease availability and cloud quotas.

## Infrastructure Indicators

- **Pod scheduling latency > 30s average or > 120s max**: cluster under pressure. May cause cascading timeouts.
- **Many pods in Failed phase**: infrastructure instability. Cross-reference with scheduling latency.

## Common Log Signatures

| Pattern | Likely Cause |
|---------|-------------|
| `timeout`, `deadline exceeded`, `context deadline` | Operation took too long -- check if timeout is too short or target is genuinely slow |
| `OOM`, `killed`, `memory`, `cannot allocate` | Resource exhaustion -- check pod resource requests/limits |
| `quota`, `limit exceeded`, `insufficient` | Cloud or cluster quota hit |
| `image pull`, `not found`, `manifest unknown` | Image reference broken or registry unavailable |
| `API incompatibility`, `no matches for kind`, `scheme` | API version skew between components |
| `connection refused`, `no route to host`, `DNS` | Network issue -- usually transient, investigate if persistent |
