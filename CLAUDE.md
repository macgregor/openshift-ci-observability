# OpenShift CI Observability

## Always-loaded context
- @README.md
- @ARCHITECTURE.md
- @CONTRIBUTING.md

## Load on demand
- docs/appendix/ci-operator-metrics.md -- when parsing or modifying metric extraction logic
- docs/appendix/gcs-bucket-layout.md -- when working with GCS paths, XML API, or artifact discovery
- docs/appendix/grafana-visualizations.md -- when building or modifying Grafana dashboards, panels, or variables
- docs/appendix/openshift-ci-infrastructure.md -- when investigating CI failures, interpreting job lifecycle phases, cluster provisioning, resource contention, or navigating external CI tooling

## Stack management
**Always use Makefile targets** (`make up`, `make down`, `make restart`, `make test`, `make build`, `make status`, `make logs`) instead of calling podman/podman-compose directly. The Makefile handles image rebuilds, profile selection, and container naming. Only fall back to direct podman commands when a specific operation has no make target (e.g. `podman exec`, `podman logs` for a specific container).

## Dashboard verification
After creating or modifying Grafana dashboards, verify them visually in the browser using chrome-devtools MCP. Don't rely solely on JSON syntax validation -- panels can be syntactically valid but render "No data", "Configure your query", or display incorrect data. Navigate to the dashboard URL, wait for panels to load, and screenshot to confirm panels render with actual data.

## Documentation quality
Run `/document-reviewer` after creating or substantially updating documentation. Skip for minor fixes (typos, single-line edits).
