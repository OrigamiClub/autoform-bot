"""Tests for host-neutral Git-ref claim leases."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

import pytest

from autoform_cli import claims


def _git(*args: str, cwd: Path | None = None, input_text: str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    return proc.stdout.strip()


@pytest.fixture
def board_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))
    return repo


def _board(tmp_path: Path, repo: Path, owner: str) -> claims.ClaimBoard:
    return claims.ClaimBoard(repo, owner, tmp_path / f"scratch-{owner}")


def test_claim_board_recognizes_scp_remote_without_explicit_user(tmp_path: Path) -> None:
    board = claims.ClaimBoard(
        "github-work:org/repo.git",
        "worker",
        tmp_path / "scratch",
    )

    assert board.repo_url == "github-work:org/repo.git"


@pytest.mark.parametrize(
    "remote",
    [
        "git@example.com:org/repo.git",
        "github-work:org/repo.git",
        "[2001:db8::1]:org/repo.git",
        "g:repo.git",
    ],
)
def test_repository_remote_grammar_matches_scp_forms(remote: str) -> None:
    assert claims._repository_is_remote(remote, windows=False)


@pytest.mark.parametrize(
    "path",
    [
        r"C:\projects\claims.git",
        "C:/projects/claims.git",
        r"\\?\C:\projects\claims.git",
        r"\\.\C:\projects\claims.git",
        r"\\server\share\claims.git",
    ],
)
def test_repository_remote_grammar_rejects_rooted_windows_paths(path: str) -> None:
    assert not claims._repository_is_remote(path, windows=False)


def test_drive_relative_path_is_local_only_on_windows() -> None:
    assert claims._repository_is_remote("C:claims.git", windows=False)
    assert not claims._repository_is_remote("C:claims.git", windows=True)


def test_claim_board_does_not_treat_windows_drive_as_scp_remote(tmp_path: Path) -> None:
    raw = r"C:\projects\claims.git"
    board = claims.ClaimBoard(
        raw,
        "worker",
        tmp_path / "scratch",
    )

    assert board.repo_url == str(Path(raw).expanduser().resolve())
    assert Path(board.repo_url).is_absolute()


def test_non_directory_scratch_is_a_clean_transport_error(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.write_text("not a directory", encoding="utf-8")
    board = claims.ClaimBoard(board_repo, "worker", scratch)

    with pytest.raises(claims.ClaimTransportError, match="prepare.*scratch"):
        board.read("author/node")


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user and mode-bit permissions",
)
def test_inaccessible_scratch_parent_is_a_clean_transport_error(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    board = claims.ClaimBoard(board_repo, "worker", locked / "scratch")
    locked.chmod(0)
    try:
        with pytest.raises(claims.ClaimTransportError, match="prepare.*scratch"):
            board.read("author/node")
    finally:
        locked.chmod(0o755)


def test_concurrent_first_use_initializes_one_shared_scratch(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    workers = 12
    start = threading.Barrier(workers)
    failures: list[BaseException] = []

    def read_missing_claim() -> None:
        try:
            start.wait(timeout=5)
            board = claims.ClaimBoard(board_repo, "worker", scratch)
            assert board.read("author/missing") is None
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=read_missing_claim) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert _git("rev-parse", "--is-bare-repository", cwd=scratch) == "true"


def _plant_message(repo: Path, key: str, message: str) -> str:
    tree = _git("mktree", cwd=repo, input_text="")
    commit = _git("commit-tree", tree, "-m", message, cwd=repo)
    _git("update-ref", claims.CLAIM_REF_PREFIX + key, commit, cwd=repo)
    return commit


def _plant_lease(repo: Path, key: str, **changes: object) -> str:
    lease: dict[str, object] = {
        "schema": claims.CLAIM_SCHEMA,
        "owner": "original-owner",
        "host": "test-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": key,
    }
    lease.update(changes)
    return _plant_message(repo, key, json.dumps(lease))


def test_acquire_read_list_and_release_round_trip(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.acquire("author/node", ttl=600, note="proof")
    lease = board.read("author/node")
    assert lease is not None
    assert lease["schema"] == claims.CLAIM_SCHEMA
    assert lease["owner"] == "worker-a"
    assert lease["resource"] == "author/node"
    assert lease["note"] == "proof"
    assert board.holds("author/node")

    listed = board.list()
    assert [(item["_key"], item["_expired"]) for item in listed] == [("author/node", False)]
    assert board.release("author/node")
    assert board.read("author/node") is None
    assert board.release("author/node")


def test_cas_acquire_race_has_exactly_one_winner(tmp_path: Path, board_repo: Path) -> None:
    boards = [_board(tmp_path, board_repo, owner) for owner in ("worker-a", "worker-b")]
    barrier = threading.Barrier(2)
    original_remote_oid = claims.ClaimBoard._remote_oid

    def synchronized_remote_oid(self: claims.ClaimBoard, key: str) -> str | None:
        oid = original_remote_oid(self, key)
        barrier.wait(timeout=5)
        return oid

    for board in boards:
        board._remote_oid = synchronized_remote_oid.__get__(board, claims.ClaimBoard)  # type: ignore[method-assign]

    results: list[bool] = []
    errors: list[BaseException] = []

    def acquire(board: claims.ClaimBoard) -> None:
        try:
            results.append(board.acquire("race", ttl=600))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=acquire, args=(board,)) for board in boards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]
    for board in boards:
        board._remote_oid = original_remote_oid.__get__(board, claims.ClaimBoard)  # type: ignore[method-assign]
    assert boards[0].read("race")["owner"] in {"worker-a", "worker-b"}


def test_expired_lease_can_be_taken_over(tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr(claims.time, "time", lambda: now)
    first = _board(tmp_path, board_repo, "worker-a")
    second = _board(tmp_path, board_repo, "worker-b")

    assert first.acquire("expired", ttl=10)
    monkeypatch.setattr(claims.time, "time", lambda: now + 11)
    assert not first.holds("expired")
    assert second.acquire("expired", ttl=60)
    assert second.read("expired")["owner"] == "worker-b"


def test_malformed_lease_is_unverifiable_and_not_takeover_eligible(tmp_path: Path, board_repo: Path) -> None:
    _plant_message(board_repo, "malformed", "not json")
    board = _board(tmp_path, board_repo, "worker-a")

    for operation in (
        lambda: board.read("malformed"),
        lambda: board.renew("malformed"),
        lambda: board.release("malformed"),
        lambda: board.acquire("malformed", ttl=600),
        lambda: board.acquire("malformed", ttl=600, steal=True),
    ):
        with pytest.raises(claims.MalformedLeaseError):
            operation()

    listed = board.list()
    assert listed[0]["schema"] == "unreadable"
    assert listed[0]["_malformed"] is True
    assert listed[0]["_expired"] is False
    assert board.cleanup() == 0


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), float("-inf")])
def test_acquire_rejects_nonfinite_ttl_without_mutating_remote(
    tmp_path: Path, board_repo: Path, ttl: float
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(ValueError, match="finite positive number"):
        board.acquire("nonfinite", ttl=ttl)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "nonfinite", cwd=board_repo) == ""


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), float("-inf")])
def test_renew_rejects_nonfinite_ttl_without_replacing_lease(
    tmp_path: Path, board_repo: Path, ttl: float
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("owned", ttl=30)
    oid = board._remote_oid("owned")

    with pytest.raises(ValueError, match="finite positive number"):
        board.renew("owned", ttl=ttl)

    assert board._remote_oid("owned") == oid


def test_owner_only_renew_and_release(tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000.0
    monkeypatch.setattr(claims.time, "time", lambda: now)
    owner = _board(tmp_path, board_repo, "owner")
    peer = _board(tmp_path, board_repo, "peer")
    assert owner.acquire("owned", ttl=30)
    first_expiry = owner.read("owned")["expires_at"]

    assert not peer.renew("owned")
    assert not peer.release("owned")
    assert not peer.acquire("owned", ttl=30)
    monkeypatch.setattr(claims.time, "time", lambda: now + 5)
    assert owner.renew("owned", ttl=30)
    assert owner.read("owned")["expires_at"] > first_expiry
    assert owner.release("owned")


def test_single_key_operations_read_only_the_exact_claim_ref(tmp_path: Path, board_repo: Path) -> None:
    # ls-remote for refs/autoform-claims/k also lists the peer's
    # refs/autoform-claims/a/refs/autoform-claims/k, which ends in the same path.
    peer = _board(tmp_path, board_repo, "peer")
    assert peer.acquire("a/refs/autoform-claims/k", ttl=600)
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.read("k") is None
    assert board.acquire("k", ttl=600)
    assert board.holds("k")
    assert board.renew("k", ttl=600)
    assert board.release("k")
    assert peer.holds("a/refs/autoform-claims/k")


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_expired_rejects_nonfinite_explicit_comparison_clock(now: float) -> None:
    lease = {"expires_at": 200.0}

    with pytest.raises(ValueError, match="comparison clock must be finite"):
        claims.ClaimBoard.expired(lease, now=now)


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_expired_rejects_nonfinite_default_comparison_clock(
    monkeypatch: pytest.MonkeyPatch, now: float
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: now)

    with pytest.raises(ValueError, match="comparison clock must be finite"):
        claims.ClaimBoard.expired({"expires_at": 200.0})


def test_cleanup_removes_only_expired_snapshot_entries(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("dead", ttl=5)
    assert board.acquire("live", ttl=500)
    monkeypatch.setattr(claims.time, "time", lambda: 1_010.0)

    assert board.cleanup() == 1
    assert [lease["_key"] for lease in board.list()] == ["live"]


def test_cleanup_cas_does_not_delete_renewed_lease(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    cleaner = _board(tmp_path, board_repo, "worker-a")
    assert cleaner.acquire("lease", ttl=5)
    monkeypatch.setattr(claims.time, "time", lambda: 1_010.0)

    original_list = cleaner.list
    owner = _board(tmp_path, board_repo, "worker-a")

    def list_then_renew() -> list[dict[str, object]]:
        snapshot = original_list()
        assert owner.renew("lease", ttl=500)
        return snapshot

    monkeypatch.setattr(cleaner, "list", list_then_renew)
    assert cleaner.cleanup() == 0
    assert cleaner.read("lease")["expires_at"] == 1_510.0


def test_author_claim_keys_are_ref_safe_and_resist_slug_collisions() -> None:
    node_ids = ["a b", "a-b", "A/B", "A B", "Évariste Galois", "!!!", "x" * 200]
    keys = [claims.author_claim_key(node_id) for node_id in node_ids]

    assert len(keys) == len(set(keys))
    assert all(key.startswith("author/") for key in keys)
    assert all(claims.CLAIM_KEY_RE.fullmatch(key) for key in keys)
    assert all(".." not in key for key in keys)


@pytest.mark.parametrize(
    "key",
    ["has space", "a/../b", "/leading", "trailing/", "refs/heads/x@{1}", ".hidden", "ends.", "lease.lock"],
)
def test_invalid_keys_are_rejected(tmp_path: Path, board_repo: Path, key: str) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    with pytest.raises(ValueError, match="invalid claim key"):
        board.acquire(key)


def test_relative_local_repo_path_is_resolved_before_entering_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))
    monkeypatch.chdir(tmp_path)
    board = claims.ClaimBoard("claims.git", "worker-a", tmp_path / "scratch")

    assert board.acquire("relative", ttl=600)
    assert board.read("relative")["owner"] == "worker-a"


def test_transport_failure_raises_without_local_fallback(tmp_path: Path) -> None:
    board = claims.ClaimBoard(tmp_path / "missing" / "claims.git", "worker-a", tmp_path / "scratch")

    with pytest.raises(claims.ClaimTransportError):
        board.acquire("key")
    assert not (board.scratch / claims.CLAIM_REF_PREFIX / "key").exists()


def test_heartbeat_verifies_ownership_immediately_on_entry() -> None:
    class LostBoard:
        def renew(self, key: str, ttl: int | float) -> bool:
            return False

    heartbeat = claims.Heartbeat(LostBoard(), "key", interval=1, ttl=30)  # type: ignore[arg-type]
    with pytest.raises(claims.ClaimTransportError, match="lost before"):
        with heartbeat:
            pytest.fail("unowned work must not enter the protected context")

    assert heartbeat.lost.is_set()
    assert heartbeat._thread is None


def test_heartbeat_rejects_interval_that_can_outlive_lease() -> None:
    with pytest.raises(ValueError, match="shorter than"):
        claims.Heartbeat(object(), "key", interval=30, ttl=30)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("interval", "ttl", "message"),
    [
        (float("nan"), 30, "heartbeat interval must be a finite positive number"),
        (float("inf"), 30, "heartbeat interval must be a finite positive number"),
        (1, float("nan"), "claim TTL must be a finite positive number"),
        (1, float("inf"), "claim TTL must be a finite positive number"),
        (1, float("-inf"), "claim TTL must be a finite positive number"),
    ],
)
def test_heartbeat_rejects_nonfinite_timing(interval: float, ttl: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        claims.Heartbeat(object(), "key", interval=interval, ttl=ttl)  # type: ignore[arg-type]


def test_heartbeat_marks_ownership_lost_on_transport_failure() -> None:
    attempted = threading.Event()

    class FailingBoard:
        calls = 0

        def renew(self, key: str, ttl: int | float) -> bool:
            self.calls += 1
            if self.calls == 1:
                return True
            attempted.set()
            raise claims.ClaimTransportError("board unavailable")

    heartbeat = claims.Heartbeat(FailingBoard(), "key", interval=0.01, ttl=30)  # type: ignore[arg-type]
    with heartbeat:
        assert attempted.wait(timeout=2)
        assert heartbeat.lost.wait(timeout=2)

    assert isinstance(heartbeat.error, claims.ClaimTransportError)


def test_heartbeat_marks_ownership_lost_when_renew_is_refused() -> None:
    attempted = threading.Event()

    class LostBoard:
        calls = 0

        def renew(self, key: str, ttl: int | float) -> bool:
            self.calls += 1
            if self.calls == 1:
                return True
            attempted.set()
            return False

    heartbeat = claims.Heartbeat(LostBoard(), "key", interval=0.01, ttl=30)  # type: ignore[arg-type]
    with heartbeat:
        assert attempted.wait(timeout=2)
        assert heartbeat.lost.wait(timeout=2)

    assert heartbeat.error is None


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "other"},
        {"resource": "different"},
        {"owner": ""},
        {"expires_at": "later"},
        {"acquired_at": float("nan")},
        {"acquired_at": float("inf")},
        {"acquired_at": float("-inf")},
        {"expires_at": float("nan")},
        {"expires_at": float("inf")},
        {"expires_at": float("-inf")},
    ],
)
def test_schema_resource_or_required_field_mismatch_is_malformed(
    tmp_path: Path,
    board_repo: Path,
    changes: dict[str, object],
) -> None:
    _plant_lease(board_repo, "wrong", **changes)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError):
        board.holds("wrong")
    with pytest.raises(claims.MalformedLeaseError):
        board.renew("wrong")
    with pytest.raises(claims.MalformedLeaseError):
        board.release("wrong")
    with pytest.raises(claims.MalformedLeaseError):
        board.acquire("wrong", ttl=600)
    assert board.list()[0]["_malformed"] is True
    assert board.cleanup() == 0


@pytest.mark.parametrize("field", ["schema", "owner", "resource", "acquired_at", "expires_at"])
def test_planted_lease_with_duplicate_decision_field_is_rejected_by_strict_json_parser(
    tmp_path: Path, board_repo: Path, field: str
) -> None:
    values = {
        "schema": '"autoform-claim/v1"',
        "owner": '"worker-a"',
        "resource": '"duplicate"',
        "acquired_at": "100.0",
        "expires_at": "200.0",
    }
    pairs = [f'"{name}":{value}' for name, value in values.items()]
    pairs.append(f'"{field}":{values[field]}')
    _plant_message(board_repo, "duplicate", "{" + ",".join(pairs) + "}")
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError, match="invalid lease JSON"):
        board.read("duplicate")

    assert board.list()[0]["_malformed"] is True


def test_planted_nonfinite_lease_is_rejected_by_strict_json_parser(
    tmp_path: Path, board_repo: Path
) -> None:
    message = (
        '{"schema":"autoform-claim/v1","owner":"worker-a","resource":"strict-json",'
        '"acquired_at":0,"expires_at":NaN}'
    )
    _plant_message(board_repo, "strict-json", message)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError, match="invalid lease JSON"):
        board.read("strict-json")


def test_acquire_rejects_nonfinite_clock_before_commit_or_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    monkeypatch.setattr(claims.time, "time", lambda: float("nan"))

    with pytest.raises(ValueError, match="claim timestamp must be finite"):
        board.acquire("bad-clock", ttl=30)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "bad-clock", cwd=board_repo) == ""


def test_acquire_rejects_nonfinite_expiry_before_commit_or_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    monkeypatch.setattr(claims.time, "time", lambda: 1e308)

    with pytest.raises(ValueError, match="claim expiry must be finite"):
        board.acquire("bad-expiry", ttl=1e308)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "bad-expiry", cwd=board_repo) == ""


def _refs(repo: Path) -> dict[str, str]:
    listing = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo)
    return dict(line.split(" ", 1) for line in listing.splitlines())


@pytest.mark.parametrize(("method", "key"), [("renew", "a"), ("release", "a"), ("acquire", "c")])
def test_list_retries_when_a_claim_ref_changes_between_ls_remote_and_fetch(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, method: str, key: str
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    before = _refs(board_repo)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    changed: list[bool] = []

    def change_before_first_fetch(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch" and not changed:
            changed.append(getattr(owner, method)(key))
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", change_before_first_fetch)
    listed = reader.list()

    assert changed == [True]
    assert _refs(board_repo) != before
    assert {claims.CLAIM_REF_PREFIX + lease["_key"]: lease["_oid"] for lease in listed} == _refs(board_repo)
    assert not any(lease["_malformed"] for lease in listed)


def test_list_returns_its_verified_snapshot_when_refs_change_while_leases_are_parsed(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    before = _refs(board_repo)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    ls_remotes: list[list[str]] = []

    def renew_while_parsing(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "ls-remote":
            ls_remotes.append(args)
        if args[:2] == ["cat-file", "commit"]:
            assert owner.renew("b")
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", renew_while_parsing)
    listed = reader.list()

    assert _refs(board_repo) != before
    assert {claims.CLAIM_REF_PREFIX + lease["_key"]: lease["_oid"] for lease in listed} == before
    assert not any(lease["_malformed"] for lease in listed)
    assert len(ls_remotes) == 2


def test_list_raises_when_claim_refs_keep_changing(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git

    def renew_after_each_ls_remote(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = git(args, **kwargs)
        if args[0] == "ls-remote":
            before = _refs(board_repo)
            assert owner.renew("a")
            assert _refs(board_repo) != before
        return proc

    monkeypatch.setattr(reader, "_git", renew_after_each_ls_remote)
    with pytest.raises(claims.ClaimTransportError, match="changed during each"):
        reader.list()


def test_list_reports_a_claim_ref_to_a_non_commit_as_malformed(tmp_path: Path, board_repo: Path) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("live", ttl=600)
    blob = _git("hash-object", "-w", "--stdin", cwd=board_repo, input_text="not a commit")
    _git("update-ref", claims.CLAIM_REF_PREFIX + "blob", blob, cwd=board_repo)

    listed = {lease["_key"]: lease for lease in _board(tmp_path, board_repo, "worker-b").list()}

    assert listed["blob"]["_malformed"] is True
    assert listed["blob"]["_oid"] == blob
    assert listed["live"]["_malformed"] is False


def test_list_reports_a_failed_fetch_of_an_unchanged_claim_without_retrying(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    fetches: list[list[str]] = []

    def failing_fetch(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch":
            fetches.append(args)
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: simulated fetch failure\n")
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", failing_fetch)
    with pytest.raises(claims.ClaimTransportError, match=r"cannot fetch claim 'a'.*simulated fetch failure"):
        reader.list()
    assert len(fetches) == 1


def test_acquire_many_is_all_or_nothing_when_a_peer_holds_one_key(tmp_path: Path, board_repo: Path) -> None:
    peer = _board(tmp_path, board_repo, "worker-b")
    assert peer.acquire("held", ttl=600)
    before = _refs(board_repo)
    board = _board(tmp_path, board_repo, "worker-a")

    result = board.acquire_many(["first", "held", "last"], ttl=600)

    assert result == claims.ClaimBatchResult(False, ("held",), "held by another worker")
    assert not result
    assert _refs(board_repo) == before


def test_acquire_many_takes_over_an_expired_key(tmp_path: Path, board_repo: Path) -> None:
    _plant_lease(board_repo, "expired", owner="worker-b")
    board = _board(tmp_path, board_repo, "worker-a")

    result = board.acquire_many(["fresh", "expired"], ttl=600, note="revision")

    assert result == claims.ClaimBatchResult(True)
    for key in ("fresh", "expired"):
        lease = board.read(key)
        assert lease["owner"] == "worker-a"
        assert lease["note"] == "revision"
        assert board.holds(key)


def test_acquire_many_changes_no_ref_when_one_lease_goes_stale_before_the_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_lease(board_repo, "contested", owner="worker-b")
    board = _board(tmp_path, board_repo, "worker-a")
    peer = _board(tmp_path, board_repo, "worker-b")
    push = board._cas_push_refs
    observed: dict[str, dict[str, str]] = {}

    def peer_renews_first(updates: Sequence[tuple[str, str | None, str]], *, atomic: bool = False):
        # The expired lease passed the ownership check; its owner renews it
        # before the push, so exactly one of the two leases is stale.
        assert peer.renew("contested", ttl=600)
        observed["refs"] = _refs(board_repo)
        return push(updates, atomic=atomic)

    monkeypatch.setattr(board, "_cas_push_refs", peer_renews_first)
    result = board.acquire_many(["free", "contested"], ttl=600)

    assert result == claims.ClaimBatchResult(False, ("contested",), "changed concurrently")
    assert _refs(board_repo) == observed["refs"]
    assert claims.CLAIM_REF_PREFIX + "free" not in observed["refs"]
    assert peer.holds("contested")


def test_acquire_many_changes_no_ref_when_the_remote_aborts_the_transaction(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    board._ensure_scratch()
    rival = _git("commit-tree", _git("mktree", cwd=board_repo, input_text=""), "-m", "rival", cwd=board_repo)
    contested = claims.CLAIM_REF_PREFIX + "contested"
    # Git runs pre-push after checking every lease against the remote's
    # advertisement, so this rival claim meets the remote's own check inside
    # the ref transaction instead.
    hooks = board.scratch / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(f'#!/bin/sh\ncat >/dev/null\nexec git --git-dir="{board_repo}" update-ref {contested} {rival}\n')
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))

    result = board.acquire_many(["free", "contested"], ttl=600)

    assert result == claims.ClaimBatchResult(False, ("contested",), "changed concurrently")
    assert _refs(board_repo) == {contested: rival}


def test_overlapping_batch_race_has_one_winner_and_no_partial_loser(tmp_path: Path, board_repo: Path) -> None:
    boards = {owner: _board(tmp_path, board_repo, owner) for owner in ("worker-a", "worker-b")}
    barrier = threading.Barrier(2)
    original_remote_oids = claims.ClaimBoard._remote_oids

    def synchronized_remote_oids(self: claims.ClaimBoard, keys: Sequence[str]) -> dict[str, str]:
        oids = original_remote_oids(self, keys)
        barrier.wait(timeout=60)
        return oids

    for board in boards.values():
        board._remote_oids = synchronized_remote_oids.__get__(board, claims.ClaimBoard)  # type: ignore[method-assign]

    results: dict[str, claims.ClaimBatchResult] = {}
    errors: list[BaseException] = []

    def acquire(owner: str) -> None:
        try:
            results[owner] = boards[owner].acquire_many([f"only-{owner}", "shared"], ttl=600)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=acquire, args=(owner,)) for owner in boards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    winners = [owner for owner, result in results.items() if result]
    assert len(winners) == 1
    winner = winners[0]
    loser = next(owner for owner in boards if owner != winner)
    assert results[loser] == claims.ClaimBatchResult(False, ("shared",), "changed concurrently")
    assert sorted(_refs(board_repo)) == [claims.CLAIM_REF_PREFIX + key for key in (f"only-{winner}", "shared")]
    assert boards[loser].read("shared")["owner"] == winner


def test_renew_many_renews_every_owned_key_and_keeps_notes(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_000.0
    monkeypatch.setattr(claims.time, "time", lambda: now)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire_many(["first", "second"], ttl=60, note="revision")
    monkeypatch.setattr(claims.time, "time", lambda: now + 30)

    assert board.renew_many(["first", "second"], ttl=600) == claims.ClaimBatchResult(True)
    for key in ("first", "second"):
        lease = board.read(key)
        assert lease["expires_at"] == now + 30 + 600
        assert lease["note"] == "revision"


def test_renew_many_renews_nothing_unless_every_key_is_owned(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    peer = _board(tmp_path, board_repo, "worker-b")
    assert board.acquire("mine", ttl=600)
    assert peer.acquire("theirs", ttl=600)
    before = _refs(board_repo)

    result = board.renew_many(["mine", "theirs", "absent"], ttl=900)

    assert result == claims.ClaimBatchResult(False, ("theirs", "absent"), "not held by this worker")
    assert _refs(board_repo) == before


def test_release_many_deletes_owned_keys_and_treats_absent_keys_as_released(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire_many(["first", "second"], ttl=600)

    assert board.release_many(["first", "absent", "second"]) == claims.ClaimBatchResult(True)
    assert _refs(board_repo) == {}
    assert board.release_many(["first", "second"]) == claims.ClaimBatchResult(True)


def test_release_many_releases_nothing_when_one_key_is_foreign(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    peer = _board(tmp_path, board_repo, "worker-b")
    assert board.acquire("mine", ttl=600)
    assert peer.acquire("theirs", ttl=600)
    before = _refs(board_repo)

    result = board.release_many(["mine", "theirs"])

    assert result == claims.ClaimBatchResult(False, ("theirs",), "not held by this worker")
    assert _refs(board_repo) == before


def test_release_many_deletes_no_ref_when_one_lease_goes_stale_before_the_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    peer = _board(tmp_path, board_repo, "worker-b")
    assert board.acquire_many(["kept", "stolen"], ttl=600)
    push = board._cas_push_refs
    observed: dict[str, dict[str, str]] = {}

    def peer_steals_first(updates: Sequence[tuple[str, str | None, str]], *, atomic: bool = False):
        assert peer.acquire("stolen", ttl=600, steal=True)
        observed["refs"] = _refs(board_repo)
        return push(updates, atomic=atomic)

    monkeypatch.setattr(board, "_cas_push_refs", peer_steals_first)
    result = board.release_many(["kept", "stolen"])

    assert result == claims.ClaimBatchResult(False, ("stolen",), "changed concurrently")
    assert _refs(board_repo) == observed["refs"]
    assert board.holds("kept")


@pytest.mark.parametrize("method", ["acquire_many", "renew_many", "release_many"])
def test_batch_names_a_malformed_lease_as_blocking_and_changes_nothing(
    tmp_path: Path, board_repo: Path, method: str
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("mine", ttl=600)
    _plant_message(board_repo, "broken", "not json")
    before = _refs(board_repo)

    result = getattr(board, method)(["mine", "broken"])

    assert result == claims.ClaimBatchResult(False, ("broken",), "malformed lease")
    assert _refs(board_repo) == before


def test_acquire_many_names_every_blocking_key_in_batch_order(tmp_path: Path, board_repo: Path) -> None:
    peer = _board(tmp_path, board_repo, "worker-b")
    assert peer.acquire("held", ttl=600)
    _plant_message(board_repo, "broken", "not json")
    before = _refs(board_repo)
    board = _board(tmp_path, board_repo, "worker-a")

    result = board.acquire_many(["broken", "free", "held"], ttl=600)

    assert result == claims.ClaimBatchResult(False, ("broken", "held"), "malformed lease or held by another worker")
    assert _refs(board_repo) == before


@pytest.mark.parametrize("method", ["acquire_many", "renew_many", "release_many"])
@pytest.mark.parametrize(
    ("keys", "error", "match"),
    [
        (["first", "second", "first"], ValueError, "duplicate claim key 'first'"),
        ([], ValueError, "at least one claim key is required"),
        (["first", "../escape"], ValueError, "invalid claim key"),
        ("first", TypeError, "not one string"),
    ],
)
def test_batch_methods_reject_bad_keys_before_touching_the_board(
    tmp_path: Path, board_repo: Path, method: str, keys: object, error: type[Exception], match: str
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(error, match=match):
        getattr(board, method)(keys)

    assert not board.scratch.exists()
    assert _refs(board_repo) == {}


def test_batch_refuses_a_board_without_atomic_push_support(tmp_path: Path, board_repo: Path) -> None:
    with (board_repo / "config").open("a", encoding="utf-8") as config:
        config.write("[receive]\n\tadvertiseAtomic = false\n")
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.ClaimTransportError, match="does not support atomic pushes"):
        board.acquire_many(["first", "second"], ttl=600)

    assert _refs(board_repo) == {}
    assert board.acquire("first", ttl=600)


def test_single_key_push_is_unchanged_and_batches_push_atomically(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    git = board._git
    pushes: list[list[str]] = []

    def recording_git(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "push":
            pushes.append(args)
        return git(args, **kwargs)

    monkeypatch.setattr(board, "_git", recording_git)
    assert board.acquire("single", ttl=600)
    assert board.acquire_many(["one", "two"], ttl=600)

    single, one, two = (claims.CLAIM_REF_PREFIX + key for key in ("single", "one", "two"))
    refs = _refs(board_repo)
    assert pushes == [
        ["push", "--quiet", "--porcelain", f"--force-with-lease={single}:", board.repo_url, f"{refs[single]}:{single}"],
        [
            "push",
            "--quiet",
            "--porcelain",
            "--atomic",
            f"--force-with-lease={one}:",
            f"--force-with-lease={two}:",
            board.repo_url,
            f"{refs[one]}:{one}",
            f"{refs[two]}:{two}",
        ],
    ]


# Old enough to outlast any grace; it formats as 2023-11-14T22:13:20Z.
_STALE_LOCK_MTIME = 1_700_000_000


def _plant_ref_lock(repo: Path, ref: str, *, modified: float = _STALE_LOCK_MTIME) -> Path:
    """Leave the lock file a Git process that stopped while updating ``ref`` would leave."""
    lock = repo / f"{ref}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    os.utime(lock, (modified, modified))
    return lock


def _new_board_repo(tmp_path: Path, ref_format: str) -> Path:
    repo = tmp_path / "claims.git"
    init = subprocess.run(
        ["git", "init", "--bare", "--quiet", f"--ref-format={ref_format}", str(repo)], capture_output=True, text=True
    )
    if init.returncode != 0:
        pytest.skip(f"this Git cannot create a {ref_format} repository: {init.stderr.strip()}")
    return repo


def _install_pre_push_hook(board: claims.ClaimBoard, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    """Run ``command`` after our lease check and before the remote's ref transaction."""
    hooks = board.scratch / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\ncat >/dev/null\nexec {command}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))


@pytest.mark.parametrize(
    ("operation", "locked"),
    [
        (lambda board: board.acquire("free", ttl=600), "free"),
        (lambda board: board.renew("held", ttl=600), "held"),
        (lambda board: board.release("held"), "held"),
        (lambda board: board.acquire_many(["other", "free"], ttl=600), "free"),
        (lambda board: board.renew_many(["held", "held-too"], ttl=600), "held"),
        (lambda board: board.release_many(["held", "held-too"]), "held"),
    ],
    ids=["acquire", "renew", "release", "acquire_many", "renew_many", "release_many"],
)
def test_a_stale_ref_lock_refuses_the_update_names_its_file_and_stays(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, operation, locked: str
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire_many(["held", "held-too"], ttl=600)
    lock = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + locked)
    before = _refs(board_repo)

    with pytest.raises(claims.ClaimRefLockedError) as raised:
        operation(board)

    assert str(raised.value) == (
        f"Git lock file {lock.resolve()} (modified 2023-11-14T22:13:20Z) blocks updates of "
        f"{claims.CLAIM_REF_PREFIX}{locked}. Git leaves a lock file behind when a process stops while "
        "updating a ref; if no Git process is using the claim board, delete it and retry"
    )
    assert [found.path for found in raised.value.locks] == [lock.resolve()]
    assert lock.exists()
    assert _refs(board_repo) == before


def test_once_the_operator_deletes_a_stale_ref_lock_the_claim_proceeds(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    board = _board(tmp_path, board_repo, "worker-a")
    lock = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "node")
    with pytest.raises(claims.ClaimRefLockedError):
        board.acquire("node", ttl=600)
    assert board.read("node") is None

    lock.unlink()

    assert board.acquire("node", ttl=600)
    assert board.holds("node")


@pytest.mark.parametrize("change", ["create", "update", "delete", "delete-packed", "batch"])
def test_a_lock_released_within_the_grace_is_a_lost_race(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 60.0)
    board = _board(tmp_path, board_repo, "worker-a")
    if change != "create":
        assert board.acquire_many(["other", "node"], ttl=600)
    before = _refs(board_repo)
    lock = _plant_ref_lock(board_repo, "packed-refs" if change == "delete-packed" else claims.CLAIM_REF_PREFIX + "node")
    existing_locks = claims.ClaimBoard._existing_locks
    seen: list[list[claims.ClaimRefLock]] = []

    def peer_push_finishes(paths: dict[Path, str]) -> list[claims.ClaimRefLock]:
        # The peer whose update held the lock when our push failed lets go
        # after the first look.
        seen.append(existing_locks(paths))
        lock.unlink(missing_ok=True)
        return seen[-1]

    monkeypatch.setattr(claims.ClaimBoard, "_existing_locks", staticmethod(peer_push_finishes))

    if change == "create":
        assert not board.acquire("node", ttl=600)
    elif change == "update":
        assert not board.renew("node", ttl=600)
    elif change == "batch":
        assert board.renew_many(["other", "node"], ttl=600) == claims.ClaimBatchResult(
            False, ("node",), "changed concurrently"
        )
    else:
        assert not board.release("node")
    assert [[found.path for found in locks] for locks in seen] == [[lock.resolve()], []]
    assert _refs(board_repo) == before


@pytest.mark.parametrize("change", ["update", "delete", "delete-packed"])
def test_a_lock_gone_before_the_first_look_is_a_lost_race(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("node", ttl=600)
    before = _refs(board_repo)
    lock = _plant_ref_lock(board_repo, "packed-refs" if change == "delete-packed" else claims.CLAIM_REF_PREFIX + "node")
    ref_store = board._ref_store

    def peer_push_finishes() -> Path | None:
        # The peer lets go between Git's refusal and the board's first look.
        lock.unlink(missing_ok=True)
        return ref_store()

    monkeypatch.setattr(board, "_ref_store", peer_push_finishes)

    assert not (board.renew("node", ttl=600) if change == "update" else board.release("node"))
    assert _refs(board_repo) == before


@pytest.mark.parametrize(
    ("visible", "old", "lost"),
    [(True, None, True), (True, "1" * 40, True), (False, None, True), (False, "1" * 40, False)],
    ids=["visible-create", "visible-update", "unseen-create", "unseen-update"],
)
def test_reference_already_exists_is_a_lost_race_unless_an_unseen_lock_may_explain_it(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, visible: bool, old: str | None, lost: bool
) -> None:
    # Git 2.53's whole refusal, whether a lost race or a lock file caused it.
    board = _board(tmp_path, board_repo, "worker-a")
    ref = claims.CLAIM_REF_PREFIX + "node"
    new = "2" * 40
    git = board._git

    def refused_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "push":
            return git(args, **kwargs)
        out = f"To board\n!\t{new}:{ref}\t[remote rejected] (reference already exists)\nDone\n"
        return subprocess.CompletedProcess(["git", *args], 1, out, "error: failed to push some refs to 'board'\n")

    monkeypatch.setattr(board, "_git", refused_push)
    if not visible:
        monkeypatch.setattr(board, "_ref_store", lambda: None)

    if lost:
        assert board._cas_push_refs([("node", old, new)]) == claims.ClaimBatchResult(
            False, ("node",), "changed concurrently"
        )
    else:
        with pytest.raises(claims.ClaimTransportError, match="reference already exists"):
            board._cas_push_refs([("node", old, new)])


@pytest.mark.parametrize("change", ["create", "update", "delete"])
def test_a_lost_race_is_refused_rather_than_an_error(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    # Git 2.53 words each of these losses differently from Git 2.50. As in the
    # atomic test above, the rival change passes our lease check and meets the
    # remote's own check inside the ref transaction.
    board = _board(tmp_path, board_repo, "worker-a")
    board._ensure_scratch()
    ref = claims.CLAIM_REF_PREFIX + "node"
    if change != "create":
        assert board.acquire("node", ttl=600)
    rival = _git("commit-tree", _git("mktree", cwd=board_repo, input_text=""), "-m", "rival", cwd=board_repo)

    if change == "delete":
        _install_pre_push_hook(board, monkeypatch, f'git --git-dir="{board_repo}" update-ref -d {ref}')
        assert not board.release("node")
        assert _refs(board_repo) == {}
    else:
        _install_pre_push_hook(board, monkeypatch, f'git --git-dir="{board_repo}" update-ref {ref} {rival}')
        assert not (board.acquire("node", ttl=600) if change == "create" else board.renew("node", ttl=600))
        assert _refs(board_repo) == {ref: rival}


def test_a_packed_refs_lock_blocks_releasing_claims(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("node", ttl=600)
    before = _refs(board_repo)
    lock = _plant_ref_lock(board_repo, "packed-refs")

    with pytest.raises(claims.ClaimRefLockedError, match="blocks releasing claims"):
        board.release("node")

    assert lock.exists()
    assert _refs(board_repo) == before


def test_a_stale_reftable_lock_blocks_every_claim_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    repo = _new_board_repo(tmp_path, "reftable")
    board = _board(tmp_path, repo, "worker-a")
    assert board.acquire("held", ttl=600)
    before = _refs(repo)
    lock = _plant_ref_lock(repo, "reftable/tables.list")

    for operation in (
        lambda: board.acquire("free", ttl=600),
        lambda: board.renew("held", ttl=600),
        lambda: board.release("held"),
    ):
        with pytest.raises(claims.ClaimRefLockedError, match="blocks every claim update"):
            operation()

    assert [(found.path, found.ref) for found in board.ref_locks()] == [(lock.resolve(), "reftable")]
    assert lock.exists()
    assert _refs(repo) == before


def test_a_stale_ref_lock_never_lets_a_takeover_replace_a_lease(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    _plant_lease(board_repo, "expired")
    _plant_lease(board_repo, "live", expires_at=2_000.0)
    before = _refs(board_repo)
    for key in ("expired", "live"):
        _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + key)
    board = _board(tmp_path, board_repo, "worker-a")

    assert not board.acquire("live", ttl=600)
    with pytest.raises(claims.ClaimRefLockedError):
        board.acquire("expired", ttl=600)
    with pytest.raises(claims.ClaimRefLockedError):
        board.acquire("live", ttl=600, steal=True)

    assert _refs(board_repo) == before


def test_a_remote_board_neither_names_nor_deletes_a_lock_file(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    assert _board(tmp_path, board_repo, "worker-a").acquire("held", ttl=600)
    remote = claims.ClaimBoard(board_repo.as_uri(), "worker-a", tmp_path / "scratch-remote")
    # Over a network transport the board's files are not visible.
    monkeypatch.setattr(remote, "_ref_store", lambda: None)
    locks = [_plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + key) for key in ("held", "free")]
    before = _refs(board_repo)

    for operation in (
        lambda: remote.acquire("free", ttl=600),
        lambda: remote.renew("held", ttl=600),
        lambda: remote.release("held"),
    ):
        # Git's own error or a lost race, depending on the Git version.
        try:
            outcome = operation()
        except claims.ClaimTransportError as error:
            assert not isinstance(error, claims.ClaimRefLockedError)
        else:
            assert outcome is False

    assert all(lock.exists() for lock in locks)
    assert _refs(board_repo) == before


@pytest.mark.parametrize(
    "board_name",
    ["claims.git", "cl\u00e2ims.git ", "claims #1?.git"],
    ids=["plain", "non-ascii-trailing-space", "hash-and-question-mark"],
)
def test_a_file_url_or_unusual_path_board_names_its_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board_name: str
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    repo = tmp_path / board_name
    _git("init", "--bare", "--quiet", str(repo))
    lock = _plant_ref_lock(repo, claims.CLAIM_REF_PREFIX + "node")

    url_path = repo.as_uri()[len("file://") :]
    # Git ignores a file URL's host, decodes it, keeps "?" and "#", drops
    # trailing slashes and finds a board named without ".git".
    locations = [
        repo,
        f"file://{url_path}",
        f"file://localhost{url_path}",
        f"file://elsewhere{url_path}",
        f"file://{repo.as_posix()}",
        f"file://{url_path}/",
    ]
    if board_name.endswith(".git"):
        locations += [str(repo)[: -len(".git")], f"file://{url_path[: -len('.git')]}/"]
    for location in locations:
        board = claims.ClaimBoard(location, "worker-a", tmp_path / "scratch")
        with pytest.raises(claims.ClaimRefLockedError, match="blocks updates of"):
            board.acquire("node", ttl=600)
        assert [found.path for found in board.ref_locks()] == [lock.resolve()]
    assert lock.exists()


def test_an_inherited_git_common_dir_does_not_hide_a_stale_lock(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    other = tmp_path / "other.git"
    _git("init", "--bare", "--quiet", str(other))
    _plant_ref_lock(other, claims.CLAIM_REF_PREFIX + "node")
    lock = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "node")
    board = _board(tmp_path, board_repo, "worker-a")
    board._ensure_scratch()
    # An inherited variable, as inside a Git hook, must not redirect the lock
    # lookup. GIT_DIR cannot, since the lookup names --git-dir itself.
    monkeypatch.setenv("GIT_COMMON_DIR", str(other))

    with pytest.raises(claims.ClaimRefLockedError) as raised:
        board.acquire("node", ttl=600)

    assert [found.path for found in raised.value.locks] == [lock.resolve()]
    assert [found.path for found in board.ref_locks()] == [lock.resolve()]


def test_a_hook_refusal_stays_an_error_even_if_a_lock_came_and_went(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 60.0)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("node", ttl=600)
    lock = _plant_ref_lock(board_repo, "packed-refs")
    existing_locks = claims.ClaimBoard._existing_locks

    def peer_lets_go(paths: dict[Path, str]) -> list[claims.ClaimRefLock]:
        found = existing_locks(paths)
        lock.unlink(missing_ok=True)
        return found

    monkeypatch.setattr(claims.ClaimBoard, "_existing_locks", staticmethod(peer_lets_go))
    git = board._git
    ref = claims.CLAIM_REF_PREFIX + "node"

    def declined_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "push":
            return git(args, **kwargs)
        # The hook's own words mention a lock and an existing reference.
        err = (
            "remote: fatal: Unable to create '/srv/mirror/.git/index.lock': File exists.\n"
            "remote: the mirror says: reference already exists\n"
            "error: failed to push some refs to 'board'\n"
        )
        out = f"To board\n!\t:{ref}\t[remote rejected] (pre-receive hook declined)\nDone\n"
        return subprocess.CompletedProcess(["git", *args], 1, out, err)

    monkeypatch.setattr(board, "_git", declined_push)

    with pytest.raises(claims.ClaimTransportError, match="pre-receive hook declined") as raised:
        board.release("node")
    assert not isinstance(raised.value, claims.ClaimRefLockedError)


def test_reference_already_exists_on_an_unseen_board_is_an_error_for_a_batch_with_an_update(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    monkeypatch.setattr(board, "_ref_store", lambda: None)
    new = "2" * 40
    created, updated = (claims.CLAIM_REF_PREFIX + key for key in ("created", "updated"))
    git = board._git

    def refused_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "push":
            return git(args, **kwargs)
        out = (
            f"To board\n!\t{new}:{created}\t[remote rejected] (atomic transaction failed)\n"
            f"!\t{new}:{updated}\t[remote rejected] (reference already exists)\nDone\n"
        )
        return subprocess.CompletedProcess(["git", *args], 1, out, "error: failed to push some refs to 'board'\n")

    monkeypatch.setattr(board, "_git", refused_push)

    with pytest.raises(claims.ClaimTransportError, match="reference already exists"):
        board._cas_push_refs([("created", None, new), ("updated", "1" * 40, new)], atomic=True)


def test_a_packed_refs_lock_leaves_a_lost_create_race_a_refusal(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    board = _board(tmp_path, board_repo, "worker-a")
    board._ensure_scratch()
    lock = _plant_ref_lock(board_repo, "packed-refs")
    rival = _git("commit-tree", _git("mktree", cwd=board_repo, input_text=""), "-m", "rival", cwd=board_repo)
    ref = claims.CLAIM_REF_PREFIX + "node"
    _install_pre_push_hook(board, monkeypatch, f'git --git-dir="{board_repo}" update-ref {ref} {rival}')

    # packed-refs.lock blocks only deletions, so a lost race to create stays one.
    assert not board.acquire("node", ttl=600)
    assert _refs(board_repo) == {ref: rival}
    assert lock.exists()


def test_a_bad_gitfile_ends_the_board_lookup_as_it_ends_git_s(tmp_path: Path) -> None:
    named = tmp_path / "board"
    named.write_text("gitdir: /nonexistent\n")
    beside = tmp_path / "board.git"
    _git("init", "--bare", "--quiet", str(beside))
    _plant_ref_lock(beside, claims.CLAIM_REF_PREFIX + "node")
    board = claims.ClaimBoard(named, "worker-a", tmp_path / "scratch")

    # Git stops at the bad gitfile instead of trying board.git.
    assert board._ref_store() is None
    assert board.ref_locks() == []


def test_a_file_url_is_decoded_before_its_host_is_dropped(tmp_path: Path) -> None:
    decoy = tmp_path / "c.git"
    real = tmp_path / f"sub{tmp_path}" / "c.git"
    for repo in (decoy, real):
        _git("init", "--bare", "--quiet", str(repo))
        _plant_ref_lock(repo, claims.CLAIM_REF_PREFIX + "node")
    # Escaped slashes in the host part become part of the path Git pushes to.
    url = "file://" + urllib.parse.quote(f"/{tmp_path}/sub", safe="") + f"{tmp_path}/c.git"
    board = claims.ClaimBoard(url, "worker-a", tmp_path / "scratch")

    assert [found.path for found in board.ref_locks()] == [(real / f"{claims.CLAIM_REF_PREFIX}node.lock").resolve()]


def test_cleanup_skips_a_lease_whose_release_a_lock_file_blocks(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    for key in ("a-locked", "b-free"):
        _plant_lease(board_repo, key)
    _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "a-locked")
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.cleanup() == 1
    assert list(_refs(board_repo)) == [claims.CLAIM_REF_PREFIX + "a-locked"]


@pytest.mark.parametrize(("ref_format", "lock_ref"), [("files", "packed-refs"), ("reftable", "reftable/tables.list")])
def test_a_lock_that_blocks_every_release_ends_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref_format: str, lock_ref: str
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    repo = _new_board_repo(tmp_path, ref_format)
    for key in ("a", "b"):
        _plant_lease(repo, key)
    _plant_ref_lock(repo, lock_ref)
    board = _board(tmp_path, repo, "worker-a")
    push = board._cas_push
    pushed: list[str] = []

    def recording_push(key: str, old: str | None, new: str) -> bool:
        pushed.append(key)
        return push(key, old, new)

    monkeypatch.setattr(board, "_cas_push", recording_push)

    assert board.cleanup() == 0
    assert pushed == ["a"]
    assert sorted(_refs(repo)) == [claims.CLAIM_REF_PREFIX + "a", claims.CLAIM_REF_PREFIX + "b"]


def test_ref_locks_lists_the_lock_files_that_can_block_claims(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 3_600.0)
    board = _board(tmp_path, board_repo, "worker-a")
    valid = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "author/node")
    malformed = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "bad..key")
    packed = _plant_ref_lock(board_repo, "packed-refs")
    # A lock younger than the grace may still belong to a push in progress;
    # one dated ahead of the clock does not.
    _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "author/fresh", modified=time.time())
    future = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "author/future", modified=time.time() + 7_200)
    (board_repo / claims.CLAIM_REF_PREFIX / "directory.lock").mkdir()
    _plant_ref_lock(board_repo, "refs/heads/main")

    assert [(lock.path, lock.ref) for lock in board.ref_locks()] == [
        (packed.resolve(), "packed-refs"),
        (future.resolve(), claims.CLAIM_REF_PREFIX + "author/future"),
        (valid.resolve(), claims.CLAIM_REF_PREFIX + "author/node"),
        (malformed.resolve(), claims.CLAIM_REF_PREFIX + "bad..key"),
    ]
    assert board.acquire("author/other", ttl=600)
    # Git reads "file:/path" as SSH to a host named "file".
    for remote in ("https://example.invalid/claims.git", f"file:{board_repo}"):
        assert claims.ClaimBoard(remote, "worker-a", tmp_path / "remote").ref_locks() == []
