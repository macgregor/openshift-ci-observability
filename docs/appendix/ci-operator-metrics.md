---
name: ci-operator-metrics
description: >
  Load when working with ci-operator-metrics.json parsing, metric extraction,
  or understanding the data structure from OpenShift CI builds.
categories: [reference, data-format]
tags: [ci-operator, json, metrics, gcs-artifacts]
related_docs:
  - docs/appendix/gcs-bucket-layout.md
complexity: intermediate
---

# ci-operator-metrics.json Reference

This document describes the structure and field meanings of the `ci-operator-metrics.json` file produced by OpenShift CI jobs.

## Source

The metrics structure is defined in the `openshift/ci-tools` repository at `pkg/metrics/`:
- `pods.go` - Pod metrics
- `nodes.go` - Node metrics
- `events.go` - Event metrics
- `builds.go` - Build metrics
- `insights.go` - Test platform insights
- `images.go` - Image metrics
- `leases.go` - Lease metrics

## Top-Level Structure

The JSON file contains the following top-level sections:
- `events`
- `images`
- `leases`
- `nodes`
- `openshift_builds`
- `pods`
- `test_platform_insights`

## Section Details

### events

Records of Kubernetes events during the CI run.

**Key Fields:**
- `level` (string): Event severity level
- `source` (string): Component that generated the event
- `locator` (object): Event target
  - `name` (string): Name of the object
- `message` (object): Event details
  - `reason` (string): Machine-readable reason code
  - `humanMessage` (string): Human-readable description
  - `annotations.duration_seconds` (string): Duration in seconds
- `from` (string): ISO 8601 timestamp of event start
- `to` (string): ISO 8601 timestamp of event end
- `timestamp` (string): ISO 8601 timestamp of event occurrence

### pods

Metrics for pods created during the CI run.

**Key Fields:**
- `pod_name` (string): Name of the pod
- `namespace` (string): Kubernetes namespace
- `creation_time` (string): ISO 8601 timestamp
- `start_time` (string): ISO 8601 timestamp
- `completion_time` (string): ISO 8601 timestamp
- `ci_workload` (string): Type of CI workload
- `scheduling_latency` (number): Nanoseconds from creation to scheduled
- `initialization_latency` (number): Nanoseconds for initialization phase
- `ready_latency` (number): Nanoseconds until pod ready
- `completion_latency` (number): Nanoseconds to completion
- `pod_phase` (string): Kubernetes pod phase (e.g., "Succeeded", "Failed")
- `condition_transition_times` (array): State transition timestamps

**Units:** All latency fields are in nanoseconds.

### nodes

Metrics for Kubernetes nodes used during the CI run.

**Key Fields:**
- `node` (string): Node name
- `arch` (string): CPU architecture (e.g., "amd64")
- `machine_type` (string): Machine type/instance class
- `machine_id` (string): Unique machine identifier
- `age_seconds` (number): Age of the node in seconds
- `resources` (object): Node resource information
  - `capacity` (object): Resource capacity with Kubernetes quantity strings (e.g., "32856996Ki" for memory)
- `usage_stats` (object): Resource usage statistics
  - `cpu_milli` (object): CPU usage in millicores with `min`, `max`, `avg`
  - `memory_bytes` (object): Memory usage in bytes with `min`, `max`, `avg`

**Units:**
- Node resources use Kubernetes quantity strings (e.g., "32856996Ki")
- CPU usage in millicores
- Memory usage in bytes

### openshift_builds

Metrics for OpenShift Build objects.

**Key Fields:**
- `namespace` (string): Build namespace
- `name` (string): Build name
- `start_time` (string): ISO 8601 timestamp
- `completion_time` (string): ISO 8601 timestamp
- `duration_seconds` (number): Build duration in seconds
- `status` (string): Build completion status
- `output_image` (string): Resulting image reference
- `for_image` (string): Target image being built

**Units:** Duration is in seconds.

### images

Image stream and image-related metrics.

**Key Fields:**
- `namespace` (string): Image stream namespace
- `image_stream_name` (string): Name of the image stream
- `full_name` (string): Full image reference
- `success` (boolean): Whether the operation succeeded
- `additional_context` (object): Extra contextual information

### leases

Resource lease metrics. This section may be empty if no leases were used.

### test_platform_insights

High-level job and test platform metadata.

**Key Fields:**
- `name` (string): Insight entry name (e.g., "started")
- `timestamp` (string): ISO 8601 timestamp
- `additional_context` (object): Context-specific data

**Special Entry: "started"**

The "started" entry contains a `job_spec` object with:
- `org` (string): GitHub organization
- `repo` (string): Repository name
- `branch` (string): Base branch
- `job` (string): Job name
- `buildid` (string): Unique build identifier
- `pulls` (array): PR information for pull request jobs

**Units:** Event duration is in seconds.

## Known Units Summary

| Field Type | Unit |
|------------|------|
| Pod latencies | nanoseconds |
| Node age | seconds |
| Node resources | Kubernetes quantity strings (e.g., "Ki", "Mi") |
| CPU usage | millicores |
| Memory usage | bytes |
| Build duration | seconds |
| Event duration | seconds |
| Timestamps | ISO 8601 strings |
