from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from autoform_cli import _directory_binding as directory_binding_module
from autoform_cli.__main__ import main
from autoform_cli._tree_snapshot import descriptor_capture_supported
from autoform_cli.lean import index_project, snapshot_project_sources
from autoform_cli.render import PUBLICATION_MANIFEST, render_site
from tests.test_render import _REPOSITORY, _commit_project, _debris, _edit, _project, _tree


@pytest.mark.skipif(os.name != "nt", reason="Windows capability boundary")
@pytest.mark.parametrize("capture", [snapshot_project_sources, index_project])
def test_windows_source_inspection_fails_closed_without_descriptor_traversal(
    tmp_path: Path,
    capture,
) -> None:
    (tmp_path / "A.lean").write_text(
        "theorem notReadUnsafely : True := trivial\n",
        encoding="utf-8",
    )

    with pytest.raises(OSError, match="safe directory traversal is unavailable"):
        capture(tmp_path)


_SKIPPED_LINKS = (
    "warning: code links were skipped because this platform cannot capture Lean "
    "sources safely (directory-descriptor traversal is unavailable); "
    "render on Linux or macOS to link declarations"
)


def _without_descriptor_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    # A no-op on Windows, where the capability is absent natively.
    monkeypatch.setattr(directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False)


@pytest.mark.parametrize("require_declarations", [False, True])
def test_render_without_descriptor_capture_publishes_without_code_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    require_declarations: bool,
) -> None:
    _without_descriptor_capture(monkeypatch)
    project = _project(tmp_path)
    commit = _commit_project(project)
    output = tmp_path / "site-src"

    def unreachable(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the implicit Lean root must not be read without descriptor capture")

    monkeypatch.setattr("autoform_cli.render.snapshot_project_sources", unreachable)
    arguments = ["render", str(project / "blueprint"), "--output", str(output)]
    arguments += ["--repository-url", _REPOSITORY, "--ref", commit]
    if require_declarations:
        arguments.append("--require-declarations")

    # The second render replaces the first site.
    for _ in range(2):
        status = main(arguments)
        stdout = capsys.readouterr().out
        assert status == (1 if require_declarations else 0)
        assert [line for line in stdout.splitlines() if line.startswith("warning:")] == [_SKIPPED_LINKS]
    assert "0 code links" in stdout
    page = (output / "roadmap" / "README.md").read_text(encoding="utf-8")
    assert "<code>Project.top</code>" in page
    assert "not found in the Lean sources" not in page
    # Links to the Markdown sources still pin the verified commit.
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["git_ref"] == commit
    assert f"{_REPOSITORY}/blob/{commit}/blueprint/roadmap/top.md" in page


def test_render_without_descriptor_capture_records_no_ref_for_an_uncommitted_blueprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_descriptor_capture(monkeypatch)
    project = _project(tmp_path)
    commit = _commit_project(project)
    _edit(project / "blueprint/roadmap/top.md", "The main result.", "The main result, revised.")
    output = tmp_path / "site-src"

    report = render_site(project / "blueprint", output, repository_url=_REPOSITORY, ref=commit)

    # No Lean byte was read, so once the blueprint differs nothing was compared with the commit.
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["git_ref"] is None
    assert "/blob/" not in (output / "roadmap" / "README.md").read_text(encoding="utf-8")
    assert any(warning.startswith("Markdown source links were omitted") for warning in report.warnings)


@pytest.mark.parametrize("require_declarations", [False, True])
def test_render_without_descriptor_capture_is_quiet_when_no_declaration_is_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    require_declarations: bool,
) -> None:
    _without_descriptor_capture(monkeypatch)
    project = _project(tmp_path)
    for article in (project / "blueprint" / "roadmap").glob("*.md"):
        text = article.read_text(encoding="utf-8")
        article.write_text(re.sub(r"(?m)^(lean|statement|proof): .*\n", "", text), encoding="utf-8")
    output = tmp_path / "site-src"
    arguments = ["render", str(project / "blueprint"), "--output", str(output)]
    if require_declarations:
        arguments.append("--require-declarations")

    status = main(arguments)

    assert status == 0
    assert "warning:" not in capsys.readouterr().out
    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["complete"]
    assert manifest["lean_source_revision"] == ""


def test_render_without_descriptor_capture_refuses_to_overwrite_a_modified_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _without_descriptor_capture(monkeypatch)
    project = _project(tmp_path)
    output = tmp_path / "site-src"
    arguments = ["render", str(project / "blueprint"), "--output", str(output)]
    assert main(arguments) == 0
    (output / "README.md").write_text("edited\n", encoding="utf-8")
    before = _tree(output)
    capsys.readouterr()

    status = main(arguments)

    assert status == 1
    assert "refusing to overwrite a modified Autoform publication" in capsys.readouterr().out
    assert _tree(output) == before
    assert not _debris(tmp_path)


def test_render_explicit_lean_root_fails_closed_without_descriptor_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _without_descriptor_capture(monkeypatch)
    project = _project(tmp_path)
    output = tmp_path / "site-src"

    status = main(
        ["render", str(project / "blueprint"), "--output", str(output), "--lean-root", str(project)]
    )

    assert status == 1
    assert capsys.readouterr().out.splitlines() == [
        "error: Lean sources could not be indexed: "
        "safe directory traversal is unavailable on this platform; "
        "run without --lean-root to publish without code links"
    ]
    assert not output.exists()


def test_render_with_descriptor_capture_still_links_the_implicit_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not descriptor_capture_supported():
        pytest.skip("directory descriptor capture is unavailable")
    project = _project(tmp_path)
    output = tmp_path / "site-src"

    status = main(
        ["render", str(project / "blueprint"), "--output", str(output), "--require-declarations"]
    )

    stdout = capsys.readouterr().out
    assert status == 0
    assert "warning:" not in stdout
    assert "2 code links" in stdout
    assert "Project/Basic.lean" in (output / "roadmap" / "README.md").read_text(encoding="utf-8")
