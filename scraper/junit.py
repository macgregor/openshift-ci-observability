"""JUnit pipeline for parsing step-level and test-case-level results."""
import json
import logging
import re
from xml.etree import ElementTree as ET

from scraper import SHARED_VERSION
from scraper.context import BuildContext
from scraper.metrics import format_prometheus_line
from scraper.models import Sink

log = logging.getLogger("scraper")

MULTI_STAGE_RE = re.compile(r"^Run multi-stage test (.+)$")


def parse_junit_xml(content: str) -> tuple[dict, list[dict]]:
    root = ET.fromstring(content)
    # Handle both <testsuites><testsuite>... and <testsuite>... roots
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    suite_attrs = dict(suite.attrib) if suite is not None else {}

    cases = []
    source = suite if suite is not None else root
    for tc in source.findall("testcase"):
        failure_el = tc.find("failure")
        skipped_el = tc.find("skipped")
        if failure_el is not None:
            status = "failed"
            failure_message = (failure_el.text or failure_el.get("message", "")).strip()
        elif skipped_el is not None:
            status = "skipped"
            failure_message = None
        else:
            status = "passed"
            failure_message = None

        cases.append({
            "name": tc.get("name", ""),
            "time": tc.get("time", "0"),
            "status": status,
            "failure_message": failure_message,
        })

    return suite_attrs, cases


def extract_test_names(cases: list[dict]) -> list[str]:
    names = []
    for c in cases:
        m = MULTI_STAGE_RE.match(c["name"])
        if m:
            names.append(m.group(1))
    return names


def filter_leaf_tests(cases: list[dict]) -> list[dict]:
    """Filter to leaf test cases only (no children).

    Go test names are hierarchical: TestFoo/Bar/Baz. Parent tests aggregate
    child results, so TestFoo always fails when any child fails. Only leaf
    nodes are useful for failure analysis and duration ranking.
    """
    names = {c["name"] for c in cases}
    return [c for c in cases if not any(
        n != c["name"] and n.startswith(c["name"] + "/") for n in names
    )]


class JunitPipeline:
    name = "junit"
    version = f"{SHARED_VERSION}.1"
    _pushes_logs = True

    def __init__(self, metrics_sink: Sink, logs_sink: Sink):
        self.metrics_sink = metrics_sink
        self.logs_sink = logs_sink

    def process(self, ctx: BuildContext) -> int:
        # Get build timestamp from started.json
        started_content = ctx.fetch_artifact("started.json")
        if started_content is None:
            log.debug("No started.json for build %s, skipping JUnit",
                      ctx.build.build_id)
            return 0
        try:
            timestamp = int(json.loads(started_content)["timestamp"])
        except (json.JSONDecodeError, KeyError, ValueError):
            log.warning("Unparseable started.json for build %s, skipping JUnit",
                        ctx.build.build_id)
            return 0

        # Fetch + parse junit_operator.xml
        operator_content = ctx.fetch_artifact("artifacts/junit_operator.xml")
        if operator_content is None:
            return 0

        suite_attrs, step_cases = parse_junit_xml(operator_content)

        metric_records = []
        log_records = []

        # Emit step-level metrics and logs
        for case in step_cases:
            try:
                duration = float(case["time"])
            except (ValueError, TypeError):
                duration = 0.0

            labels = {
                **ctx.labels,
                "step_name": case["name"],
                "status": case["status"],
            }
            line = format_prometheus_line(
                "ci_junit_step_duration_seconds", labels, round(duration, 3), timestamp
            )
            if line:
                metric_records.append(line)

            if case["status"] == "failed" and case["failure_message"]:
                record = {
                    **ctx.labels,
                    "_time": timestamp,
                    "_msg": case["failure_message"],
                    "source": "junit_step",
                    "pipeline": "junit",
                    "step_name": case["name"],
                    "status": "failed",
                    "duration_seconds": round(duration, 3),
                }
                log_records.append(json.dumps(record))

        # Extract test names from "Run multi-stage test X" cases
        test_names = extract_test_names(step_cases)

        # For each test name, fetch + parse junit_report.xml
        for test_name in test_names:
            report_path = f"artifacts/{test_name}/e2e/artifacts/junit_report.xml"
            report_content = ctx.fetch_artifact(report_path)
            if report_content is None:
                continue

            report_attrs, test_cases = parse_junit_xml(report_content)
            suite_name = report_attrs.get("name", "")
            leaf_cases = filter_leaf_tests(test_cases)
            leaf_names = {c["name"] for c in leaf_cases}

            for case in test_cases:
                try:
                    duration = float(case["time"])
                except (ValueError, TypeError):
                    duration = 0.0

                labels = {
                    **ctx.labels,
                    "test_name": case["name"],
                    "suite": suite_name,
                    "status": case["status"],
                    "test_variant": test_name,
                    "leaf": "true" if case["name"] in leaf_names else "false",
                }
                line = format_prometheus_line(
                    "ci_junit_test_duration_seconds", labels, round(duration, 3), timestamp
                )
                if line:
                    metric_records.append(line)

                if case["status"] == "failed" and case["failure_message"]:
                    record = {
                        **ctx.labels,
                        "_time": timestamp,
                        "_msg": case["failure_message"],
                        "source": "junit_test",
                        "pipeline": "junit",
                        "test_name": case["name"],
                        "status": "failed",
                        "test_variant": test_name,
                        "duration_seconds": round(duration, 3),
                    }
                    log_records.append(json.dumps(record))

        self.metrics_sink.push(metric_records)
        self.logs_sink.push(log_records)

        total = len(metric_records) + len(log_records)
        return total
