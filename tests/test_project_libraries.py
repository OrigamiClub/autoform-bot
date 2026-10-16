from __future__ import annotations

import copy
import json
import re
from importlib.resources import files

import pytest

from autoform_cli.project import (
    LIBRARY_CATALOG_SCHEMA,
    ProjectCatalogError,
    load_library_catalog,
    load_release_catalog,
    parse_library_catalog,
)

_BUNDLED = json.loads(files("autoform_cli.project").joinpath("libraries.json").read_text(encoding="utf-8"))
_RELEASES = load_release_catalog()


def _release_package_names(release_id: str) -> set[str]:
    descriptor = json.loads(
        files("autoform_cli.project").joinpath(f"creation-release-{release_id}.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        files("autoform_cli.project").joinpath(descriptor["lake_manifest"]).read_text(encoding="utf-8")
    )
    return {entry["name"].casefold() for entry in manifest["packages"]}


def test_bundled_atlas_entry_is_offered_at_its_published_index() -> None:
    atlas = load_library_catalog().get("atlas")

    assert atlas is not None
    assert atlas.git == "https://github.com/facebookresearch/atlas-lean"
    # The commit the tag autoform-index/64c2773ffad3 adds the index in.
    assert atlas.commit == "d2d6fc8d34f246eb9acdcca2c1c4085800db6d74"
    assert atlas.release == "lean-v4.34.1-mathlib-v4.34.1"
    assert atlas.offered is True
    assert set(atlas.module_roots) == {
        "MathlibExt", "CSLibExt", "WantedExt", "MathlibExtTest", "CSLibExtTest", "Cslib", "CslibTests"
    }
    assert [entry["name"] for entry in atlas.manifest_packages] == ["atlas", "cslib"]


def test_every_entry_is_pinned_on_a_listed_release_and_locks_its_own_require() -> None:
    release_ids = {release.id for release in _RELEASES.releases}
    for library in load_library_catalog().libraries:
        assert library.release in release_ids
        assert re.fullmatch(r"[0-9a-f]{40}", library.commit)
        direct = [entry for entry in library.manifest_packages if not entry["inherited"]]
        assert direct == [entry for entry in library.manifest_packages if entry["name"] == library.name]
        assert direct[0]["url"] == library.git
        assert direct[0]["rev"] == direct[0]["inputRev"] == library.commit
        # Offline proxy for "every package its require brings in": the entries the
        # release does not already lock. Task 5's real build proves completeness.
        added = {entry["name"].casefold() for entry in library.manifest_packages}
        assert not added & _release_package_names(library.release)


def _mutated(path: tuple[object, ...], value: object) -> dict:
    payload = copy.deepcopy(_BUNDLED)
    target = payload
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return payload


_ATLAS = ("libraries", 0)


@pytest.mark.parametrize(
    "payload",
    [
        _mutated(("schema",), "autoform-project-library-catalog/v0"),
        _mutated(("extra",), 1),
        _mutated(("libraries",), {}),
        _mutated(("libraries",), ["atlas"]),
        _mutated((*_ATLAS, "extra"), 1),
        _mutated((*_ATLAS, "offered"), KeyError),
        _mutated((*_ATLAS, "name"), "at las"),
        _mutated((*_ATLAS, "git"), "http://github.com/zibo-yang/atlas-lean"),
        _mutated((*_ATLAS, "git"), 'https://github.com/x/"y'),
        _mutated((*_ATLAS, "git"), "https://github.com/x/y\\z"),
        _mutated((*_ATLAS, "commit"), "0CA59C787215C3638311B0A14AF59AAAE3208F6B"),
        _mutated((*_ATLAS, "commit"), "0ca59c78"),
        _mutated((*_ATLAS, "release"), "lean-v9.9.9-mathlib-v9.9.9"),
        _mutated((*_ATLAS, "offered"), "no"),
        _mutated((*_ATLAS, "module_roots"), []),
        _mutated((*_ATLAS, "module_roots"), "MathlibExt"),
        _mutated((*_ATLAS, "module_roots"), ["mathlibExt"]),
        _mutated((*_ATLAS, "module_roots"), ["Cslib", "CSLIB"]),
        _mutated((*_ATLAS, "manifest_packages"), []),
        _mutated((*_ATLAS, "manifest_packages", 0, "rev"), "1" * 40),
        _mutated((*_ATLAS, "manifest_packages", 0, "inputRev"), "main"),
        _mutated((*_ATLAS, "manifest_packages", 0, "url"), "https://github.com/zibo-yang/atlas-lean"),
        _mutated((*_ATLAS, "manifest_packages", 1, "inherited"), False),
        _mutated((*_ATLAS, "manifest_packages", 0), "atlas"),
    ],
)
def test_malformed_catalog_is_refused(payload: dict) -> None:
    with pytest.raises(ProjectCatalogError):
        parse_library_catalog(payload, _RELEASES)


def test_duplicate_library_names_are_refused() -> None:
    payload = copy.deepcopy(_BUNDLED)
    payload["libraries"].append(copy.deepcopy(payload["libraries"][0]))
    with pytest.raises(ProjectCatalogError):
        parse_library_catalog(payload, _RELEASES)


def test_bundled_catalog_uses_the_current_schema() -> None:
    assert _BUNDLED["schema"] == LIBRARY_CATALOG_SCHEMA
