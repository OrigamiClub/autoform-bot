"""Opt-in integration tests against the pinned upstream Lean REPL."""

from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import suppress
from pathlib import Path

import pytest

from servers.lean_client import LeanRuntimeClient, LeanRuntimeUnavailable
from servers.repl.core import LeanRepl, LeanReplConfig


REPL_FIXTURE = Path(__file__).parent / "fixtures" / "repl-smoke"
MINIMUM_LEAN = Path(__file__).parent / "fixtures" / "minimum-lean"
pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOFORM_RUN_REAL_REPL_TESTS") != "1",
    reason="set AUTOFORM_RUN_REAL_REPL_TESTS=1 to run pinned REPL integration",
)


def test_disposable_call_matches_the_pinned_repl_protocol():
    repl = LeanRepl(LeanReplConfig(cwd=str(REPL_FIXTURE), warmup_imports=frozenset(), validate_imports=False))

    response = repl.run_disposable("theorem autoform_repl_probe : True := by sorry", timeout=180)

    assert response.get("sorries")
    assert "env" not in response
    assert all("proofState" not in sorry for sorry in response["sorries"])
    assert repl.is_clean()


def test_fixture_exposes_escaped_module_traversal_without_policy_validation():
    repl = LeanRepl(
        LeanReplConfig(
            cwd=str(REPL_FIXTURE),
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )

    response = repl.run_disposable(
        "import Mathlib.«..».Secret\n#check escapedTraversalMarker",
        timeout=180,
    )

    assert "repl_error" not in response
    assert not any(
        message["severity"] == "error" for message in response.get("messages", [])
    )
    assert repl.is_clean()


@pytest.mark.parametrize(
    ("warmup", "code", "expected_error"),
    [
        ((), "/- note -/\nimport Init.Data\n#check Nat", "Disallowed imports: Init"),
        ((), "module\npublic import Init.Data\n", "Disallowed imports: Init"),
        (("REPL",), "/- note -/ import Init.Data\n#check Nat", "Disallowed imports: Init"),
        ((), "import NotAllowlisted.Mod\n", "Disallowed imports: NotAllowlisted"),
        ((), "import «REPL\n", "Rejected Lean header"),
        ((), "/- a --/\nimport REPL.Frontend\n-- -/\n#check Nat", None),
        ((), "/- a --/\nimport Unsafe\n-- -/\n#check Nat", "Disallowed imports: Unsafe"),
        ((), "/- /---/ -/\nimport Unsafe\n-- -/\n#check Nat\n", "Disallowed imports: Unsafe"),
        (
            (),
            "/- a --/\n/- x /--/ -/\nimport Unsafe\n-- -/\n#check Nat\n",
            "Disallowed imports: Unsafe",
        ),
        (
            (),
            "prelude -- ordinary comment\n"
            "import Init /- a --/\n"
            "import Lean\n"
            "-- -/\n"
            "#check Lean.Name",
            "Disallowed imports: Lean",
        ),
        ((), "module\nimport allMathlib\n", "Disallowed imports: allMathlib"),
        (
            (),
            "import Mathlib.«..».Secret\n#check escapedTraversalMarker",
            "plain, nonempty module components",
        ),
        # Name.toString leaves these unescaped, so each root would read as Mathlib or Aesop.
        ((), "import «Mathlib.Unsafe».«x✝»\n#check Nat", "plain, nonempty module components"),
        ((), "import «Mathlib.Unsafe»._hyg\n#check Nat", "plain, nonempty module components"),
        ((), "import «Mathlib.Unsafe».«_inaccessible»\n#check Nat", "plain, nonempty module components"),
        ((), "import «Mathlib.Unsafe✝»\n#check Nat", "plain, nonempty module components"),
        ((), "import «Aesop.Unsafe».«x✝»\n#check Nat", "plain, nonempty module components"),
        ((), "import REPL.Frontend\n#check Nat", None),
        ((), '#eval "--/"', None),
        ((), "--/ harmless line comment\n#check Nat", None),
        ((), "#check Nat\n/- body --/", None),
        (("Mathlib",), "#check autoformWarmupMarker", None),
        (
            ("Mathlib",),
            "import Mathlib.TraversalAnchor\n#check autoformWarmupMarker",
            None,
        ),
        (("Mathlib",), "module\npublic import REPL.Frontend\n", None),
        (("Mathlib",), "prelude\nimport REPL.Frontend\n#check Nat", None),
    ],
)
def test_disposable_imports_are_checked_by_lean(warmup, code, expected_error):
    repl = LeanRepl(
        LeanReplConfig(
            cwd=str(REPL_FIXTURE),
            allowed_imports=frozenset({"Mathlib", "REPL"}),
            warmup_imports=frozenset(warmup),
        )
    )

    response = repl.run_disposable(code, timeout=180)

    if expected_error is None:
        assert "repl_error" not in response
        assert not any(message["severity"] == "error" for message in response.get("messages", []))
    else:
        assert expected_error in response["repl_error"]
    assert repl.is_clean()


@pytest.mark.parametrize(
    "code",
    [
        "#check autoformWarmupMarker",
        "module\npublic import REPL.Frontend\n",
        "prelude\nimport REPL.Frontend\n#check Nat",
    ],
)
def test_unvalidated_calls_use_warmup_only_when_composition_is_proven(code):
    repl = LeanRepl(
        LeanReplConfig(
            cwd=str(REPL_FIXTURE),
            validate_imports=False,
        )
    )

    response = repl.run_disposable(code, timeout=180)

    assert "repl_error" not in response
    assert not any(
        message["severity"] == "error" for message in response.get("messages", [])
    )
    assert repl.is_clean()


@pytest.mark.parametrize(("code", "is_prelude"), [("#check Nat", False), ("prelude\nimport Init", True)])
def test_header_parser_runs_on_the_minimum_supported_lean(code, is_prelude):
    # The default header command, run in a directory pinned to the oldest
    # supported Lean. Before v4.30 both headers report one plain Init import,
    # so only isPrelude tells them apart.
    completed = subprocess.run(
        LeanReplConfig().header_command,
        cwd=MINIMUM_LEAN,
        input=code.encode(),
        capture_output=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout) == {
        "imports": [
            {
                "errors": [],
                "result": {
                    "imports": [{"module": "Init", "importAll": False, "isExported": True, "isMeta": False}],
                    "isModule": False,
                    "isPrelude": is_prelude,
                },
            }
        ]
    }


def test_runtime_calls_use_distinct_disposable_processes(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "1")
    client = LeanRuntimeClient(socket_path=runtime_dir / "real-repl.sock", response_timeout=300, startup_timeout=30)
    params = {
        "project_dir": str(REPL_FIXTURE),
        "code": "#eval IO.Process.getPID",
        "timeout": 180,
    }
    try:
        responses = [client.request("repl.run", params) for _ in range(2)]
    finally:
        with suppress(LeanRuntimeUnavailable):
            client.stop()

    process_ids = []
    for response in responses:
        match = re.search(r"info: (\d+)", response)
        assert match is not None, response
        process_ids.append(int(match.group(1)))
    assert process_ids[0] != process_ids[1]


def test_runtime_warmup_prefix_preserves_source_line_numbers(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "1")
    client = LeanRuntimeClient(
        socket_path=runtime_dir / "real-repl-lines.sock",
        response_timeout=300,
        startup_timeout=30,
    )
    try:
        response = client.request(
            "repl.run",
            {
                "project_dir": str(REPL_FIXTURE),
                "code": "#check autoformWarmupMarker\n#check autoformMissingIdentifier",
                "timeout": 180,
            },
        )
    finally:
        with suppress(LeanRuntimeUnavailable):
            client.stop()

    # Without the warmup prefix the marker is unknown as well, and without the
    # line shift the remaining error would be reported on line 3.
    assert "Compilation Errors (1)" in response
    assert re.search(r"\b2:\d+: error:", response), response
