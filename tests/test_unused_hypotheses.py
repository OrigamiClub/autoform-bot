from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from autoform_cli import unused_hypotheses
from autoform_cli.__main__ import main
from autoform_cli.skeleton import SkeletonError, _lean_requests, run_probe
from autoform_cli.unused_hypotheses import (
    PROBE_MARKER,
    find_unused_hypotheses,
    format_unused_hypotheses,
    parse_probe_output,
    render_probe,
)
from tests.test_skeleton import _blueprint, _build, _lean_toolchain_available, _project


def _record(root: str, **fields: object) -> str:
    return PROBE_MARKER + json.dumps({"root": root, **fields})


def _theorem(root: str, *hypotheses: tuple[int, str, str, bool], proof: str = "proved") -> str:
    listed = [
        {"position": position, "name": name, "type": type_, "used": used}
        for position, name, type_, used in hypotheses
    ]
    return _record(root, found=True, proof=proof, hypotheses=listed)


def _definition(root: str) -> str:
    return _record(root, found=True, proof="none", hypotheses=[])


def test_the_probe_lifts_lean_s_command_heartbeat_limit() -> None:
    # All declarations run in one command; under Lean's default limit a real
    # project ran out of heartbeats. The CLI's timeout bounds the run instead.
    probe = render_probe(imports=("Skel",), roots=("Skel.heavy",))

    assert "set_option maxHeartbeats 0 in\nrun_cmd do" in probe
    # The roots reach Lean as one JSON string, not a list literal.
    assert f"AutoformUnusedProbe.requestedRoots {_lean_requests(('Skel.heavy',))}" in probe


def _entry(**fields: object) -> list[dict[str, object]]:
    return [{"position": 1, "name": "h", "type": "P", "used": False, **fields}]


def test_parse_skips_lean_s_own_output() -> None:
    output = "\n".join(["info: something Lean printed", _theorem("A.p"), "warning: unused variable"])

    assert set(parse_probe_output(output, expected_roots=("A.p",))) == {"A.p"}


def test_parse_breaks_lines_only_at_newlines() -> None:
    # Lean's JSON printer leaves U+0085, U+2028 and U+2029 unescaped, and
    # `str.splitlines` would split a record at each of them.
    root = "A.«p\x85q\u2028r\u2029s»"
    output = PROBE_MARKER + json.dumps(json.loads(_theorem(root).removeprefix(PROBE_MARKER)), ensure_ascii=False)

    assert set(parse_probe_output(output, expected_roots=(root,))) == {root}


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([_record("B.q", found=False)], "unrequested declaration"),
        ([_record("A.p", found=False), _record("A.p", found=False)], "twice"),
        ([], "no answer for A.p"),
        ([PROBE_MARKER + "{not json"], "not JSON"),
        ([PROBE_MARKER + '{"found": false}'], "without a root name"),
        ([_record("A.p", found="yes")], "non-boolean found"),
        ([_record("A.p", found=True, proof="maybe", hypotheses=[])], "unknown proof state"),
        ([_theorem("A.p", (1, "h", "P", False), proof="sorry")], "without reading its proof"),
        ([_record("A.p", found=True, proof="proved")], "invalid fields"),
        ([_record("A.p", found=True, proof="proved", hypotheses={})], "malformed hypotheses"),
        ([_record("A.p", found=True, proof="proved", hypotheses=_entry(extra=1))], "malformed hypothesis entry"),
        ([_record("A.p", found=True, proof="proved", hypotheses=_entry(position=0))], "malformed hypothesis entry"),
        ([_record("A.p", found=True, proof="proved", hypotheses=_entry(position=True))], "malformed hypothesis entry"),
        ([_record("A.p", found=True, proof="proved", hypotheses=_entry(name=""))], "malformed hypothesis entry"),
        ([_record("A.p", found=True, proof="proved", hypotheses=_entry(type=""))], "malformed hypothesis entry"),
        ([_record("A.p", found=True, proof="proved", hypotheses=_entry(used=1))], "malformed hypothesis entry"),
    ],
    ids=[
        "answer-for-another-name",
        "two-answers",
        "no-answer",
        "not-json",
        "no-root",
        "non-boolean-found",
        "unknown-proof-state",
        "hypotheses-of-an-unread-proof",
        "missing-field",
        "hypotheses-not-a-list",
        "extra-entry-field",
        "position-zero",
        "position-boolean",
        "empty-name",
        "empty-type",
        "used-not-boolean",
    ],
)
def test_parse_refuses_answers_that_do_not_fit_the_question(lines: list[str], message: str) -> None:
    with pytest.raises(SkeletonError, match=message):
        parse_probe_output("\n".join(lines), expected_roots=("A.p",))


def test_an_unused_hypothesis_is_a_finding_that_does_not_fail(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lean = {
        "eligible": "Skel.Eligible",
        "heavy": "Skel.heavy_of_weight",
        "one": "Skel.NonAmbiguous",
        "pending": "Skel.observation_determined",
        "used": "Skel.heavy",
    }
    blueprint = _blueprint(tmp_path, lean=lean)
    output = "\n".join(
        [
            _definition("Skel.Eligible"),
            # Lean's printer wraps a long type; the finding keeps it on one line.
            _theorem("Skel.heavy_of_weight", (3, "h", "0 <\n    y", False), (4, "_", "Fact p", False)),
            _theorem("Skel.NonAmbiguous", (1, "h", "P", False)),
            _theorem("Skel.observation_determined", proof="sorry"),
            _theorem("Skel.heavy", (1, "h", "P", True)),
        ]
    )

    report = find_unused_hypotheses(blueprint, lean_root=project, runner=lambda probe, root: output)

    # The source may state a hypothesis its theorem does not need: a reviewer
    # decides, so the run passes.
    assert report.clean
    assert report.findings == (
        "basics/heavy: Skel.heavy_of_weight: hypothesis-unused: the proof never uses h : 0 < y",
        "basics/heavy: Skel.heavy_of_weight: hypothesis-unused: the proof never uses argument 4 : Fact p",
        "basics/one: Skel.NonAmbiguous: hypothesis-unused: the proof never uses h : P",
    )
    text = format_unused_hypotheses(report)
    assert "  Skel.heavy_of_weight: 2 of 2 hypotheses unused\n" in text
    assert "  Skel.observation_determined: the proof contains sorry, so it was not read\n" in text
    assert "  Skel.Eligible: not a theorem\n" in text
    assert "  Skel.NonAmbiguous: 1 of 1 hypothesis unused\n" in text
    # A theorem whose hypotheses are all used needs no summary.
    assert "  Skel.heavy\n" in text
    assert "\nerror:" not in text


def test_unresolved_declarations_are_reported_and_fail(tmp_path: Path) -> None:
    project = _project(tmp_path)
    # `zabsent` is unresolved before Lean runs and `heavy` after, so only sorting
    # puts them in article order.
    blueprint = _blueprint(tmp_path, lean={"zabsent": "Skel.absent", "heavy": "Skel.heavy_of_weight, Skel.Eligible"})
    output = "\n".join([_record("Skel.heavy_of_weight", found=False), _definition("Skel.Eligible")])

    report = find_unused_hypotheses(blueprint, lean_root=project, runner=lambda probe, root: output)

    # The same words as `autoform skeleton`.
    messages = [
        "basics/heavy: Skel.heavy_of_weight: not in the built environment after importing Skel.Main; "
        "check the `lean:` target and declaring source",
        "basics/zabsent: Skel.absent: declaration not found in the Lean sources",
    ]
    assert [item.message for item in report.unresolved] == messages
    assert [item.name for item in report.declarations] == ["Skel.Eligible"]
    assert not report.clean
    text = format_unused_hypotheses(report)
    assert all(f"error: {message}\n" in text for message in messages)


def test_an_alias_is_refused_without_running_lean(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "Skel" / "Aliased.lean").write_text("namespace Skel\nalias aliased := heavy\nend Skel\n", encoding="utf-8")
    # An alias the index can follow is refused too: its own proof is the same.
    with (project / "Skel" / "Defs.lean").open("a", encoding="utf-8") as source:
        source.write("namespace Skel\nalias aliasedHere := heavy\nend Skel\n")
    blueprint = _blueprint(tmp_path, lean={"aliased": "Skel.aliased", "here": "Skel.aliasedHere"})

    def runner(probe: str, root: Path) -> str:
        raise AssertionError("an alias's own proof only names its target, so Lean must not run")

    report = find_unused_hypotheses(blueprint, lean_root=project, runner=runner)

    assert [item.message for item in report.unresolved] == [
        "basics/aliased: Skel.aliased: an alias, whose own proof only names its target",
        "basics/here: Skel.aliasedHere: an alias, whose own proof only names its target",
    ]
    assert not report.clean


def test_nothing_placed_reports_without_running_lean(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"absent": "Skel.absent"})

    def runner(probe: str, root: Path) -> str:
        raise AssertionError("no declaration was placed, so Lean must not run")

    report = find_unused_hypotheses(blueprint, lean_root=project, runner=runner)

    assert [item.declaration for item in report.unresolved] == ["Skel.absent"]


def test_a_name_several_files_declare_is_unresolved_without_running_lean(tmp_path: Path) -> None:
    project = _project(tmp_path)
    for module, prefix in (("Adup", "private "), ("Bdup", "")):
        (project / "Skel" / f"{module}.lean").write_text(
            f"namespace Skel\n{prefix}theorem dup : True := sorry\nend Skel\n", encoding="utf-8"
        )
    blueprint = _blueprint(tmp_path, lean={"dup": "Skel.dup"})

    def runner(probe: str, root: Path) -> str:
        raise AssertionError("a name several files declare must not reach Lean")

    report = find_unused_hypotheses(blueprint, lean_root=project, runner=runner)

    assert [(item.declaration, item.reason) for item in report.unresolved] == [
        ("Skel.dup", "several source files declare it: Skel/Adup.lean, Skel/Bdup.lean")
    ]


def test_the_probe_imports_only_the_modules_it_reads_and_narrows_to_the_selection(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"heavy": "Skel.heavy_of_weight", "unambiguous": "Skel.NonAmbiguous"})
    asked: list[str] = []

    def runner(probe: str, root: Path) -> str:
        asked.append(probe)
        return _theorem("Skel.heavy_of_weight")

    report = find_unused_hypotheses(blueprint, lean_root=project, node_ids=("basics/heavy",), runner=runner)

    assert [item.node_id for item in report.declarations] == ["basics/heavy"]
    # Lake checks the freshness of everything imported, so nothing else is.
    assert [line for line in asked[0].splitlines() if line.startswith("import Skel")] == ["import Skel.Main"]
    assert "NonAmbiguous" not in asked[0]


def test_a_declaration_two_articles_name_is_read_once(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"a": "Skel.heavy_of_weight", "b": "Skel.heavy_of_weight"})

    def runner(probe: str, root: Path) -> str:
        # The requested root appears once in the probe's JSON roots.
        assert probe.count('\\"Skel.heavy_of_weight\\"') == 1
        return _theorem("Skel.heavy_of_weight", (3, "h", "0 < y", False))

    report = find_unused_hypotheses(blueprint, lean_root=project, runner=runner)

    assert [item.node_id for item in report.declarations] == ["basics/a", "basics/b"]


def test_duplicate_targets_are_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"heavy": "Skel.heavy_of_weight, Skel.heavy_of_weight"})

    with pytest.raises(SkeletonError, match="duplicate Lean declaration target"):
        find_unused_hypotheses(blueprint, lean_root=project, runner=lambda probe, root: "")


@pytest.mark.parametrize(
    ("edited", "message"),
    [
        ("project/Skel/Main.lean", "Lean sources changed while the unused-hypotheses probe ran"),
        ("project/lakefile.toml", "Lean project configuration changed while the unused-hypotheses probe ran"),
        ("blueprint/roadmap/basics/heavy.md", "the blueprint changed while the unused-hypotheses probe ran"),
    ],
    ids=["sources", "lake-configuration", "blueprint"],
)
def test_inputs_edited_while_lean_runs_are_refused(tmp_path: Path, edited: str, message: str) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"heavy": "Skel.heavy_of_weight"})

    def runner(probe: str, root: Path) -> str:
        with (tmp_path / edited).open("a", encoding="utf-8") as handle:
            handle.write("\n")
        return _theorem("Skel.heavy_of_weight")

    with pytest.raises(SkeletonError, match=message):
        find_unused_hypotheses(blueprint, lean_root=project, runner=runner)


def test_cli_exit_codes_json_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"heavy": "Skel.heavy_of_weight"})
    answers = iter(
        [
            _theorem("Skel.heavy_of_weight", (3, "h", "0 < y", False)),
            _theorem("Skel.heavy_of_weight", (3, "h", "0 < y", False)),
            _record("Skel.heavy_of_weight", found=False),
        ]
    )
    timeouts: list[float] = []

    def fake_run_probe(probe: str, root: Path, *, timeout: float, label: str) -> str:
        timeouts.append(timeout)
        return next(answers)

    monkeypatch.setattr(unused_hypotheses, "run_probe", fake_run_probe)
    command = ["unused-hypotheses", str(blueprint), "--lean-root", str(project)]

    assert main([*command, "--json", "--timeout", "30"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "autoform-unused-hypotheses/v1"
    assert payload["declarations"][0]["hypotheses"] == [{"name": "h", "position": 3, "type": "0 < y", "used": False}]
    assert main(command) == 0
    assert "finding: basics/heavy: Skel.heavy_of_weight: hypothesis-unused" in capsys.readouterr().out
    # A name Lean cannot find fails the run; a finding does not.
    assert main(command) == 1
    assert timeouts == [30.0, unused_hypotheses.DEFAULT_PROBE_TIMEOUT, unused_hypotheses.DEFAULT_PROBE_TIMEOUT]
    assert main([*command, "--node", "nope"]) == 2


def test_cli_names_the_command_when_lake_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"heavy": "Skel.heavy_of_weight"})
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert main(["unused-hypotheses", str(blueprint), "--lean-root", str(project)]) == 2
    assert "running the unused-hypotheses probe" in capsys.readouterr().err


def test_cli_reports_a_lean_root_that_cannot_be_resolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _blueprint(tmp_path, lean={"heavy": "Skel.heavy_of_weight"})

    assert main(["unused-hypotheses", str(blueprint), "--lean-root", "~nosuchuser-autoform/x"]) == 2
    assert capsys.readouterr().err == "error: Lean root cannot be resolved\n"


_UNUSED = """\
import Skel.Main

namespace Skel

variable {Y : Type}

class PosFact (n : Nat) : Prop where
  pos : 0 < n

-- The hypothesis is not used.
theorem needless (y : Y) (h : y = y) : y = y := rfl
-- `same` mentions `h`, so `h` cannot be deleted on its own; `same` can.
theorem chain (n : Nat) (h : 0 < n) (same : h = h) : n = n := rfl
-- The conclusion mentions `h`, so it cannot be deleted.
theorem stated (p : Prop) (h : p) : h = h := rfl
-- A proof that is another theorem may use anything: nothing is claimed unused.
theorem via : ∀ (y : Nat) (h : y = y), y = y := needless
-- Two instance hypotheses of one type, whose names no reader wrote.
theorem inst (n : Nat) [PosFact n] [PosFact n] : 0 < n + 1 := Nat.succ_pos n
-- An auto-param hypothesis.
theorem auto (n : Nat) (h : 0 < n + 1 := by omega) : True := trivial
-- The term of a `sorry` uses nothing, so it says nothing.
theorem pending (n : Nat) (h : 0 < n) : 0 < n := sorry
-- `m` is not a proposition, so it is not a hypothesis.
theorem extra (n m : Nat) (h : n = n) : n = n := rfl
-- A complete proof that uses an open statement is read.
theorem conditional (n : Nat) (h : 0 < n) (u : n = n) : 0 < n := pending n h
-- Lean's unused-variables linter skips these two; the command does not.
theorem underscored (n : Nat) (_h : n = n) : True := trivial
theorem boundedForall : ∀ n > 0, n = n := fun _ _ => rfl

section
variable {p : Nat} [PosFact p]
-- A section instance the proof never needs, and the `omit` that removes it.
theorem sectionInst (h : p = p) : p = p := h
omit [PosFact p] in
theorem sectionOmit (h : p = p) : p = p := h
end

end Skel
"""


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_probe_reads_more_roots_than_lean_compiles_in_a_list(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build(project)
    # Lean's code generator gives up on a list literal past about 1,500 entries.
    absent = tuple(f"Skel.absent{index}" for index in range(3000))
    roots = ("Skel.heavy_of_weight", *absent)

    records = parse_probe_output(run_probe(render_probe(imports=("Skel",), roots=roots), project), expected_roots=roots)

    assert records["Skel.heavy_of_weight"]["found"] is True
    assert not any(records[root]["found"] for root in absent)


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_unused_hypotheses_in_a_built_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "Skel" / "Unused.lean").write_text(_UNUSED, encoding="utf-8")
    with (project / "Skel.lean").open("a", encoding="utf-8") as handle:
        handle.write("import Skel.Unused\n")
    _build(project)
    names = (
        "Eligible auto boundedForall chain conditional extra heavy_of_weight inst needless pending"
        " sectionInst sectionOmit stated underscored via"
    ).split()
    blueprint = _blueprint(tmp_path, lean={name.lower(): f"Skel.{name}" for name in names})

    report = find_unused_hypotheses(blueprint, lean_root=project)

    assert report.unresolved == ()
    read = {
        item.name: (item.proof, [(h.position, h.name, h.type, h.used) for h in item.hypotheses])
        for item in report.declarations
    }
    assert read == {
        "Skel.needless": ("proved", [(3, "h", "y = y", False)]),
        "Skel.heavy_of_weight": ("proved", [(4, "h", "0 < Skel.HasWeight.weight y", True)]),
        "Skel.chain": ("proved", [(3, "same", "h = h", False)]),
        "Skel.stated": ("proved", []),
        "Skel.via": ("proved", [(2, "h", "y = y", True)]),
        "Skel.inst": ("proved", [(2, "_", "Skel.PosFact n", False), (3, "_", "Skel.PosFact n", False)]),
        "Skel.auto": ("proved", [(2, "h", "0 < n + 1", False)]),
        "Skel.pending": ("sorry", []),
        "Skel.extra": ("proved", [(3, "h", "n = n", False)]),
        "Skel.conditional": ("proved", [(2, "h", "0 < n", True), (3, "u", "n = n", False)]),
        "Skel.underscored": ("proved", [(2, "_h", "n = n", False)]),
        "Skel.boundedForall": ("proved", [(2, "_", "n > 0", False)]),
        "Skel.sectionInst": ("proved", [(2, "_", "Skel.PosFact p", False), (3, "h", "p = p", True)]),
        "Skel.sectionOmit": ("proved", [(2, "h", "p = p", True)]),
        "Skel.Eligible": ("none", []),
    }
    inst = next(item for item in report.declarations if item.name == "Skel.inst")
    assert inst.findings == (
        "the proof never uses argument 2 : Skel.PosFact n",
        "the proof never uses argument 3 : Skel.PosFact n",
    )


def _nested(depth: int) -> str:
    term = "x"
    for _ in range(depth):
        term = f"sqr ({term})"
    return term


_DEEP = f"""\
import Skel.Main

namespace Skel

def Q (_ : Nat) : Prop := True
theorem qAll (n : Nat) : Q n := trivial
def sqr (y : Nat) : Nat := y * y
def R {{n : Nat}} (_ : Q n) : Prop := True
theorem rAll {{n : Nat}} (q : Q n) : R q := trivial

-- Shared subterms 40 deep, in the proof and in the statement. Walking either as
-- a tree instead of a graph takes hours.
theorem dagProof (x : Nat) (h : x = x) : Q ({_nested(40)}) := by
  unfold sqr; exact qAll _
theorem dagStatement (x : Nat) (h : x = x) : R (show Q ({_nested(40)}) by unfold sqr; exact qAll _) := rAll _

end Skel
"""


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_deeply_shared_terms_are_read_in_time(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "Skel" / "Deep.lean").write_text(_DEEP, encoding="utf-8")
    with (project / "Skel.lean").open("a", encoding="utf-8") as handle:
        handle.write("import Skel.Deep\n")
    _build(project)
    blueprint = _blueprint(tmp_path, lean={"proof": "Skel.dagProof", "statement": "Skel.dagStatement"})

    # A tree walk takes hours, so 300 s still catches one and tolerates a loaded machine.
    report = find_unused_hypotheses(blueprint, lean_root=project, timeout=300)

    assert [finding.split(": ", 2)[2] for finding in report.findings] == [
        "hypothesis-unused: the proof never uses h : x = x",
        "hypothesis-unused: the proof never uses h : x = x",
    ]
