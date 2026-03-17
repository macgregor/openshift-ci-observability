---
name: gcs-bucket-layout
description: >
  Load when working with GCS path construction, XML API listing,
  or debugging scraper navigation of the test-platform-results bucket.
categories: [reference, infrastructure]
tags: [gcs, xml-api, bucket, ci-artifacts]
related_docs:
  - docs/appendix/ci-operator-metrics.md
complexity: basic
---

# GCS Bucket Layout

This document describes the Google Cloud Storage bucket structure used by OpenShift CI for storing build artifacts and how to navigate it programmatically.

## Bucket Information

**Bucket Name:** `test-platform-results`

**Base URL:** `https://storage.googleapis.com/test-platform-results/`

## Directory Structure

CI build artifacts are organized in a hierarchical directory structure:

```
pr-logs/pull/{org}_{repo}/{pr_number}/{job_name}/{build_id}/
```

### Path Components

- `{org}_{repo}`: GitHub organization and repository, separated by underscore (e.g., `opendatahub-io_opendatahub-operator`)
- `{pr_number}`: Pull request number (e.g., `3260`)
- `{job_name}`: CI job name (e.g., `pull-ci-opendatahub-io-opendatahub-operator-main-opendatahub-operator-e2e`)
- `{build_id}`: Unique build identifier (e.g., `2032076068386508800`)

### Example Path

```
pr-logs/pull/opendatahub-io_opendatahub-operator/3260/pull-ci-opendatahub-io-opendatahub-operator-main-opendatahub-operator-e2e/2032076068386508800/artifacts/ci-operator-metrics.json
```

## Build Directory Contents

Each build directory (`{build_path}/`) contains Prow-produced metadata and an `artifacts/` subdirectory with ci-operator output.

### Build-level files

```
{build_path}/
├── started.json            # start timestamp, PR info, repo refs
├── finished.json           # end timestamp, pass/fail, metadata
├── build-log.txt           # raw ci-operator stdout/stderr (text)
├── podinfo.json            # full Prow pod spec (K8s Pod JSON)
├── prowjob.json            # full ProwJob custom resource (K8s JSON)
├── prowjob_junit.xml       # overall job timeout test result (JUnit)
├── sidecar-logs.json       # Prow sidecar logs: artifact censoring and upload (JSON lines)
└── artifacts/              # ci-operator produced artifacts (see below)
```

- **`started.json`** — contains a Unix `timestamp` used for date-range filtering, plus PR number and repo commit refs. This is the first file fetched per build.
- **`finished.json`** — contains a `passed` boolean, end `timestamp`, and metadata including the work namespace and pod name.
- **`build-log.txt`** — unstructured text log of the entire ci-operator run. Typically 10-50KB.
- **`podinfo.json`** — full K8s Pod JSON for the Prow job pod, including labels, resource requests, and container specs. Typically 50-100KB.
- **`prowjob.json`** — full ProwJob custom resource with job config, refs, and status. Typically 5-15KB.
- **`prowjob_junit.xml`** — single-testcase JUnit XML recording whether the job completed before its timeout.
- **`sidecar-logs.json`** — JSON lines from the Prow sidecar process that handles artifact upload and secret censoring. Small.

### Artifact directory

```
{build_path}/artifacts/
├── ci-operator.log                  # structured JSON log (scraped)
├── ci-operator-metrics.json         # structured metrics (scraped)
├── ci-operator-step-graph.json      # step execution DAG (JSON)
├── junit_operator.xml               # JUnit results from ci-operator
├── metadata.json                    # repo/commit metadata
├── build-logs/                      # per-image container build logs
│   └── {image-name}.log            #   one text log per built image
├── build-resources/                 # K8s resource snapshots (JSON)
│   ├── builds.json
│   ├── events.json
│   ├── imagestreams.json
│   ├── pods.json
│   └── ...                         #   also: clusterClaim, clusterDeployment, templateinstances
├── release/                         # release image import step
│   ├── build-log.txt
│   ├── finished.json
│   ├── sidecar-logs.json
│   └── artifacts/
│       └── release-images-*         #   imported release image metadata
└── {test-name}/                     # one per test container (e.g. opendatahub-operator-e2e)
    ├── {step}/                      #   each step (e2e, install, ipi-install-rbac, ...)
    │   ├── build-log.txt            #     step execution log (text)
    │   ├── finished.json            #     step completion status
    │   ├── sidecar-logs.json        #     sidecar logs for this step
    │   └── artifacts/               #     step-produced artifacts (optional)
    │       └── junit_report.xml     #       JUnit results from this step
    ├── gather-extra/                #   cluster state dump post-test
    │   ├── build-log.txt
    │   ├── finished.json
    │   ├── sidecar-logs.json
    │   └── artifacts/               #     K8s resource dumps + diagnostics
    │       ├── *.json               #       events, pods, nodes, configmaps, etc.
    │       ├── audit_logs/
    │       ├── inspect/
    │       ├── network/
    │       ├── nodes/
    │       └── oc_cmds/
    ├── gather-audit-logs/           #   audit log collection step
    └── gather-must-gather/          #   must-gather collection step
```

**Currently scraped:**
- **`ci-operator-metrics.json`** — JSON with sections for events, pods, nodes, openshift_builds, images, leases, and test_platform_insights. Each section is an array of entries with numeric metrics and string labels. Typically 5-100KB. See [ci-operator-metrics.md](ci-operator-metrics.md) for the field reference.
- **`ci-operator.log`** — JSON lines, one entry per log statement from ci-operator. Fields include `time`, `msg`, `level` (info/debug/error/warning/trace), and `component`. Typically 40-600KB.
- **`junit_operator.xml`** — JUnit XML aggregating test results from ci-operator-managed steps. One testcase per step with duration and pass/fail status. Failed steps include a failure message. Typically 2-10KB.
- **`{test-name}/{step}/artifacts/junit_report.xml`** — per-step JUnit results. The `e2e` step's JUnit is typically the richest, containing individual test case pass/fail with duration. Test names discovered from "Run multi-stage test {X}" entries in `junit_operator.xml`.
- **`build-resources/clusterClaim.json`**, **`clusterDeployment.json`** — cluster pool lifecycle data: claim timing, pool name, namespace, and cluster deployment status. Used to track pool checkout duration and cluster provisioning.
- **`{test-name}/gather-extra/artifacts/metrics/prometheus.tar`** — Prometheus TSDB dump from the test cluster. Contains WAL segments and head chunks with cluster utilization metrics (CPU, memory, node roles). Typically 60-500MB.
- **`ci-operator-step-graph.json`** — DAG of step dependencies with execution status. Typically 50-150KB. Used to compute a `config_hash` label (SHA256 of structural fields: step names, descriptions, dependencies) that tracks CI config changes across builds. Step-level details are also pushed to VictoriaLogs for cross-referencing.

**Available for future use:**
- **`metadata.json`** — repo, commit, work namespace, and pod name. Small.
- **`build-logs/{image}.log`** — text build output for each container image built by ci-operator. One file per image (e.g., `src-amd64.log`, `opendatahub-operator-bundle.log`).
- **`build-resources/`** (other files) — JSON snapshots of K8s resources in the ci-operator work namespace at completion: builds, events, imagestreams, pods, templateinstances. Useful for debugging resource-level failures.
- **`{test-name}/gather-extra/artifacts/`** (other files) — post-test cluster diagnostics. Contains JSON dumps of cluster resources (events, pods, nodes, configmaps, CSVs, etc.), audit logs, and `oc` command output. Can be very large (10-30MB for the full gather).

## XML API Navigation

The GCS bucket can be navigated using the XML API (S3-compatible listing API).

### Request Parameters

- `prefix`: Directory prefix to list (e.g., `pr-logs/pull/org_repo/1234/`)
- `delimiter=/`: Treats `/` as a directory separator, enabling hierarchical navigation

### Response Format

The API returns XML with the following structure:

**XML Namespace:** `http://doc.s3.amazonaws.com/2006-03-01`

**Key Elements:**
- `<CommonPrefixes>`: Represents subdirectories when using `delimiter=/`
- `<Prefix>`: The subdirectory path within a `<CommonPrefixes>` element
- `<Contents>`: Represents individual files

### Pagination

When results exceed a single page:

- `<IsTruncated>true</IsTruncated>`: Indicates more results are available
- `<NextMarker>`: Token for the next page
- Use `marker` query parameter with the `NextMarker` value to retrieve the next page

### Example Request

```
GET https://storage.googleapis.com/test-platform-results/?prefix=pr-logs/pull/org_repo/1234/&delimiter=/
```

This returns a list of job directories for PR 1234.

### Example XML Response Structure

```xml
<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
  <Name>test-platform-results</Name>
  <Prefix>pr-logs/pull/org_repo/1234/</Prefix>
  <Delimiter>/</Delimiter>
  <IsTruncated>false</IsTruncated>
  <CommonPrefixes>
    <Prefix>pr-logs/pull/org_repo/1234/job-name-1/</Prefix>
  </CommonPrefixes>
  <CommonPrefixes>
    <Prefix>pr-logs/pull/org_repo/1234/job-name-2/</Prefix>
  </CommonPrefixes>
</ListBucketResult>
```

## Navigation Strategy

To efficiently navigate the bucket:

1. Start with a base prefix (e.g., `pr-logs/pull/org_repo/pr_number/`)
2. Use `delimiter=/` to list subdirectories
3. Parse `<CommonPrefixes>` elements to find subdirectories
4. Descend into each subdirectory by appending to the prefix
5. Handle pagination when `<IsTruncated>` is true
6. At the build level, fetch `started.json` to check timestamps before downloading full metrics
