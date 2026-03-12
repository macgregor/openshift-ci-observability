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

## Key Files at Build Level

Each build directory contains the following key files:

### started.json

Small JSON file containing build start information, including a Unix timestamp. Used for date filtering.

**Location:** `{build_path}/started.json`

### finished.json

Contains build completion status and metadata.

**Location:** `{build_path}/finished.json`

### ci-operator-metrics.json

Rich structured metrics file containing detailed information about the CI run. This is the primary file ingested by this scraper.

**Location:** `{build_path}/artifacts/ci-operator-metrics.json`

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
