"""Value objects and Protocol interfaces for the scraper domain model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypedDict


class JobLabels(TypedDict):
    org: str
    repo: str
    branch: str
    job_name: str
    pr_number: str
    pr_sha: str
    author: str
    build_id: str


@dataclass
class Build:
    build_id: str
    pr: str
    job: str
    base_path: str


class Pipeline(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def process(self, ctx: BuildContext) -> int: ...


class Sink(Protocol):
    def push(self, records: list[str]) -> None: ...


