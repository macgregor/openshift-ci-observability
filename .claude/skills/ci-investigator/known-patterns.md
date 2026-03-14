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

## Hibernation Resume Failures

Clusters from Hive pools hibernate when idle and resume on claim. The resume process has a known gap: Hive declares a cluster "ready" when the API server responds, but kube-controller-manager may need several more minutes to sync its informer caches. During this window, controllers (SA controller, replicaset controller, etc.) don't process events.

**Detection signals:**
- `serviceaccount "default" not found` in MULTIPLE namespaces (not just one): the SA controller isn't running, not a single-namespace race
- Must-gather failing with SA errors in `openshift-must-gather-*` namespaces (different from the test namespace): confirms cluster-wide controller issue
- `Authorization error (user=system:kube-apiserver, verb=get, resource=nodes, subresource=proxy)`: node proxy failures during resume
- `the server is currently unable to handle the request`: API extensions not yet loaded
- `the server doesn't have a resource type "routes"`: CRDs not registered yet
- Cluster operator `lastTransitionTime` values from hours/days before the test: stale conditions from pre-hibernation, never refreshed after resume
- Shorter claim-to-ready times on broken clusters vs healthy ones: broken clusters resume fast but aren't fully initialized, healthy clusters waited longer (giving kcm time)

**Key distinguishing test:** If the SA error appears in the must-gather namespace (created by ci-operator's post-step, completely separate from the test namespace), the SA controller is non-functional cluster-wide. This rules out a simple namespace-creation race condition.

**Correlation with disk usage:** Clusters with >=80% master disk usage take longer for kcm to sync after resume (more etcd data to replay, more informer entries to sync). The disk warning predicts the failure but doesn't cause it directly.

## Common Log Signatures

| Pattern | Likely Cause |
|---------|-------------|
| `timeout`, `deadline exceeded`, `context deadline` | Operation took too long -- check if timeout is too short or target is genuinely slow |
| `OOM`, `killed`, `memory`, `cannot allocate` | Resource exhaustion -- check pod resource requests/limits |
| `quota`, `limit exceeded`, `insufficient` | Cloud or cluster quota hit |
| `image pull`, `not found`, `manifest unknown` | Image reference broken or registry unavailable |
| `API incompatibility`, `no matches for kind`, `scheme` | API version skew between components |
| `connection refused`, `no route to host`, `DNS` | Network issue -- usually transient, investigate if persistent |
| `serviceaccount.*not found` (single namespace) | Race condition: namespace created but SA controller hasn't processed it yet. Usually resolves with a brief wait. |
| `serviceaccount.*not found` (multiple namespaces) | Hibernation resume issue: kcm informer caches not synced. See "Hibernation Resume Failures" above. |
| `volume percentage greater than or equal to 80` | Master node disk pressure. Correlates with slow kcm resume. Check if cluster is from a Hive pool. |

## Investigation Techniques

- **Classify SA failures**: Use `error-impact 'serviceaccount.*default.*not found'` to find affected builds, then check each for must-gather SA errors (different namespace = broken cluster) vs install-only SA errors (possible race condition).
- **Check claim-to-ready times**: Search logs for `"claimed cluster.*ready after"` and compare broken vs healthy builds. Faster claim = more likely a resume issue.
- **Verify operator status freshness**: Cluster operator conditions may be stale after hibernation resume. Check `lastTransitionTime` -- if it's hours before the test ran, the status is from pre-hibernation and doesn't reflect current state.
- **Cross-reference disk warnings with SA failures**: `error-impact 'volume percentage' 14d` finds builds with disk warnings. If 100% correlation with SA failures, the underlying issue is likely kcm resume timing, not disk itself.
