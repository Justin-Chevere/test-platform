import pytest

from app.runner import Outcome, ReportError, parse_junit
from app.runner.junit import MAX_MESSAGE_CHARS

# What pytest --junitxml writes, trimmed to one test of each outcome.
PYTEST_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4" time="0.5">
    <testcase classname="tests.test_api" name="test_ok" time="0.010" />
    <testcase classname="tests.test_api" name="test_bad" time="0.020">
      <failure message="AssertionError: assert 1 == 2">def test_bad():
&gt;       assert 1 == 2
E       AssertionError</failure>
    </testcase>
    <testcase classname="tests.test_db" name="test_setup_crash" time="0.003">
      <error message="failed on setup with &quot;ConnectionError&quot;">traceback</error>
    </testcase>
    <testcase classname="tests.test_db" name="test_later" time="0.000">
      <skipped type="pytest.skip" message="not ready">tests/test_db.py:9: not ready</skipped>
    </testcase>
  </testsuite>
</testsuites>
"""

# "Billion laughs": each entity expands to ten of the one before. The real attack nests
# nine levels, so a few hundred bytes of XML become gigabytes in memory.
ENTITY_BOMB = """<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<testsuites>&lol3;</testsuites>
"""


def _write(tmp_path, text):
    path = tmp_path / "report.xml"
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_every_outcome(tmp_path):
    cases = parse_junit(_write(tmp_path, PYTEST_REPORT))

    assert [(c.classname, c.name, c.outcome) for c in cases] == [
        ("tests.test_api", "test_ok", Outcome.PASSED),
        ("tests.test_api", "test_bad", Outcome.FAILED),
        ("tests.test_db", "test_setup_crash", Outcome.ERROR),
        ("tests.test_db", "test_later", Outcome.SKIPPED),
    ]
    assert cases[0].duration_seconds == 0.01
    assert cases[0].message is None
    assert cases[1].message == "AssertionError: assert 1 == 2"
    assert cases[2].message == 'failed on setup with "ConnectionError"'
    assert cases[3].message == "not ready"


def test_finds_cases_in_nested_suites_under_a_testsuite_root(tmp_path):
    report = """<testsuite name="all">
      <testsuite name="unit"><testcase classname="a" name="one"/></testsuite>
      <testsuite name="integration">
        <testsuite name="db"><testcase classname="b" name="two"/></testsuite>
      </testsuite>
    </testsuite>"""

    assert [c.name for c in parse_junit(_write(tmp_path, report))] == ["one", "two"]


def test_message_falls_back_to_the_text_and_is_clipped(tmp_path):
    report = f"""<testsuites><testsuite>
      <testcase classname="a" name="no_message"><failure>  boom  </failure></testcase>
      <testcase classname="a" name="huge"><failure message="{"x" * 10_000}"/></testcase>
    </testsuite></testsuites>"""

    cases = parse_junit(_write(tmp_path, report))

    assert cases[0].message == "boom"
    assert cases[1].message == "x" * MAX_MESSAGE_CHARS


@pytest.mark.parametrize(
    ("time_attribute", "expected"),
    [
        ('time="1.5"', 1.5),
        ("", 0.0),
        ('time="1,234.5"', 0.0),
        ('time="-3"', 0.0),
        ('time="nan"', 0.0),
    ],
)
def test_unusable_durations_count_as_zero(tmp_path, time_attribute, expected):
    report = f'<testsuites><testcase classname="a" name="t" {time_attribute}/></testsuites>'

    assert parse_junit(_write(tmp_path, report))[0].duration_seconds == expected


@pytest.mark.parametrize("text", ["", "this is not xml", "<html><body>Oops</body></html>"])
def test_unreadable_reports_raise(tmp_path, text):
    with pytest.raises(ReportError):
        parse_junit(_write(tmp_path, text))


def test_refuses_entity_expansion(tmp_path):
    with pytest.raises(ReportError):
        parse_junit(_write(tmp_path, ENTITY_BOMB))
