"""Reads JUnit XML, the test report format almost every test tool can write.

pytest (--junitxml), Jest (jest-junit), Go (go-junit-report) and Maven all produce it, and
Jenkins and GitLab read it, so supporting this one format covers most projects.
"""

import math
from pathlib import Path
from xml.etree.ElementTree import Element

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, parse

from app.runner.base import CaseResult, Outcome

# Stack traces can be huge, and their start says what went wrong.
MAX_MESSAGE_CHARS = 4000

# A test case can carry more than one of these (a failure, then an error in its teardown).
# The first one found, in this order, decides its outcome.
_OUTCOME_TAGS = (
    ("failure", Outcome.FAILED),
    ("error", Outcome.ERROR),
    ("skipped", Outcome.SKIPPED),
)


class ReportError(Exception):
    """The report exists but isn't JUnit XML that can be read."""


def parse_junit(path: Path) -> list[CaseResult]:
    # defusedxml rather than the standard library parser: the file was written by the code
    # under test, and defusedxml refuses XML tricks such as entity expansion bombs.
    try:
        root = parse(path).getroot()
    except (ParseError, DefusedXmlException) as exc:
        raise ReportError(f"not valid XML: {exc}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise ReportError(f"not a JUnit report: the root element is <{root.tag}>")
    # iter() finds test cases at any depth, since some tools nest suites inside suites.
    return [_case(element) for element in root.iter("testcase")]


def _case(element: Element) -> CaseResult:
    outcome, message = Outcome.PASSED, None
    for tag, tag_outcome in _OUTCOME_TAGS:
        detail = element.find(tag)
        if detail is not None:
            outcome = tag_outcome
            message = detail.get("message") or (detail.text or "").strip() or None
            break
    return CaseResult(
        classname=element.get("classname", ""),
        name=element.get("name", ""),
        outcome=outcome,
        duration_seconds=_seconds(element.get("time")),
        message=message[:MAX_MESSAGE_CHARS] if message else None,
    )


def _seconds(value: str | None) -> float:
    try:
        seconds = float(value or 0)
    except ValueError:  # e.g. "1,234.5" from a tool that formats numbers for people
        return 0.0
    return seconds if math.isfinite(seconds) and seconds >= 0 else 0.0
