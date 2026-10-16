"""Host-neutral, fail-closed Git-ref leases for cooperative work claims.

Each claim is stored under ``refs/autoform-claims/`` and points to an orphan
commit whose message is the lease JSON. Mutations use an exact observed object
ID as a compare-and-swap precondition, so concurrent claimants cannot silently
overwrite one another.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

CLAIM_REF_PREFIX = "refs/autoform-claims/"
CLAIM_SCHEMA = "autoform-claim/v1"
CLAIM_TTL_S = 1500
CLAIM_HEARTBEAT_S = 300
CLAIM_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_SCP_REPOSITORY_RE = re.compile(r"^(?:[^/@:]+@)?(?:\[[^\]]+\]|[^/:]+):.+$")
_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_ROOTED_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCRATCH_INIT_ATTEMPTS = 10
_SCRATCH_INIT_DELAY_S = 0.05
_LIST_ATTEMPTS = 3
# A peer's push holds a ref's lock file for milliseconds; one that outlasts
# this grace after a failed push is reported as blocking the claim.
_LOCK_GRACE_S = 2.0
_LOCK_POLL_S = 0.05
# What `git rev-parse --local-env-vars` prints: variables that point Git at
# another repository than the one the working directory names.
_LOCAL_GIT_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
    }
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "autoform",
    "GIT_AUTHOR_EMAIL": "autoform@localhost",
    "GIT_COMMITTER_NAME": "autoform",
    "GIT_COMMITTER_EMAIL": "autoform@localhost",
}
_CAS_REJECTIONS = (
    "stale info",
    "fetch first",
    "remote ref updated since checkout",
    "cannot lock ref",
    # Git 2.53, unlike 2.50, words a lost race to update or delete a ref with
    # only one of these.
    "incorrect old value provided",
    "reference does not exist",
)
# Git 2.53 words a lost race to create a ref with only this, and also any push
# a ref's lock file blocks, so where lock files are not visible it counts as a
# lost race only for a create.
_LOST_CREATE = "reference already exists"
# How Git reports a lock file, such as packed-refs.lock, that it could not take.
_LOCK_FILE_EXISTS_RE = re.compile(r"Unable to create '([^']*)\.lock': File exists")
# When the remote aborts an atomic push, every ref reports "atomic transaction
# failed"; only this error line names the ref whose expected value was gone.
_LOCK_FAILURE_RE = re.compile(r"cannot lock ref '([^']+)'")


class ClaimTransportError(RuntimeError):
    """A claim board operation could not be completed or verified."""


class MalformedLeaseError(ClaimTransportError):
    """A claim ref exists, but its lease cannot be verified safely."""


class _ClaimRefChanged(ClaimTransportError):
    """A listed claim ref moved or was deleted before its lease was read."""


@dataclass(frozen=True, slots=True)
class ClaimRefLock:
    """A Git lock file in a local claim board.

    ``ref`` is the claim ref it guards, ``packed-refs``, which blocks every
    deletion, or ``reftable``, which blocks every update of a reftable board.
    """

    path: Path
    ref: str
    modified: float

    def describe(self) -> str:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.modified))
        if self.ref == "reftable":
            blocked = "every claim update"
        elif self.ref == "packed-refs":
            blocked = "releasing claims"
        else:
            blocked = f"updates of {self.ref}"
        return f"Git lock file {self.path} (modified {stamp}) blocks {blocked}"


class ClaimRefLockedError(ClaimTransportError):
    """A Git lock file on a local claim board blocks a claim update."""

    def __init__(self, message: str, locks: Sequence[ClaimRefLock]) -> None:
        super().__init__(message)
        self.locks = tuple(locks)


@dataclass(frozen=True, slots=True)
class ClaimBatchResult:
    """The outcome of an all-or-nothing multi-key claim operation.

    It is true exactly when the operation succeeded for every key. Otherwise no
    key changed, ``reason`` says why, and ``blocking`` names the keys
    responsible when known.
    """

    ok: bool
    blocking: tuple[str, ...] = ()
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not CLAIM_KEY_RE.fullmatch(key) or ".." in key:
        raise ValueError(f"invalid claim key {key!r}")
    parts = key.split("/")
    if any(part.startswith(".") or part.endswith(".") or part.endswith(".lock") for part in parts):
        raise ValueError(f"invalid claim key {key!r}")
    return key


def _validate_keys(keys: Sequence[str]) -> tuple[str, ...]:
    if isinstance(keys, str):
        raise TypeError("claim keys must be a sequence of keys, not one string")
    batch = tuple(_validate_key(key) for key in keys)
    if not batch:
        raise ValueError("at least one claim key is required")
    seen: set[str] = set()
    for key in batch:
        if key in seen:
            raise ValueError(f"duplicate claim key {key!r}")
        seen.add(key)
    return batch


def _rejected_keys(detail: str, keys_by_ref: Mapping[str, str], markers: Sequence[str]) -> tuple[str, ...]:
    """Return the keys a refused push reports as stale or contended, in push order."""
    refs = set(_LOCK_FAILURE_RE.findall(detail))
    for line in detail.splitlines():
        # Porcelain status lines read "<flag>\t<from>:<to>\t<summary>".
        flag, _, rest = line.partition("\t")
        refspec, _, summary = rest.partition("\t")
        if flag == "!" and any(marker in summary.lower() for marker in markers):
            refs.add(refspec.rpartition(":")[2])
    return tuple(key for ref, key in keys_by_ref.items() if ref in refs)


def _refusal(blocked: Mapping[str, str]) -> ClaimBatchResult:
    """Refuse a batch before pushing, naming each blocking key in batch order and every reason."""
    return ClaimBatchResult(False, tuple(blocked), " or ".join(dict.fromkeys(blocked.values())))


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _validate_ttl(ttl: int | float) -> int | float:
    if not _is_finite_number(ttl) or ttl <= 0:
        raise ValueError("claim TTL must be a finite positive number")
    return ttl


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def author_claim_key(node_id: str) -> str:
    """Return a readable, ref-safe, collision-resistant author claim key."""
    if not isinstance(node_id, str):
        raise TypeError("node_id must be a string")
    slug = re.sub(r"[^a-z0-9-]+", "-", node_id.lower()).strip("-")[:48] or "node"
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
    return f"author/{slug}-{digest}"


def _repository_is_remote(value: str, *, windows: bool | None = None) -> bool:
    """Match Git's URL/SCP forms without misclassifying Windows paths."""

    on_windows = os.name == "nt" if windows is None else windows
    windows_path = (
        value.startswith("\\")
        or _WINDOWS_ROOTED_DRIVE_RE.match(value) is not None
        or (on_windows and _WINDOWS_DRIVE_PREFIX_RE.match(value) is not None)
    )
    return not windows_path and (
        "://" in value or _SCP_REPOSITORY_RE.fullmatch(value) is not None
    )


class ClaimBoard:
    """Lease operations against a Git repository via a local bare object store."""

    def __init__(self, repo_url: str | os.PathLike[str], worker_id: str, scratch: str | os.PathLike[str]):
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        raw_repo_url = os.fspath(repo_url)
        if not _repository_is_remote(raw_repo_url):
            raw_repo_url = str(Path(raw_repo_url).expanduser().resolve())
        self.repo_url = raw_repo_url
        self.worker_id = worker_id
        self.scratch = Path(scratch)

    def _git(
        self,
        args: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.scratch,
                capture_output=True,
                text=True,
                input=input_text,
                timeout=120,
                env={**os.environ, **_GIT_ENV},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaimTransportError(f"git claim-board operation failed: {exc}") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(f"git {' '.join(args[:2])} failed against claim board: {detail}")
        return proc

    def _ensure_scratch(self) -> None:
        last_error: BaseException | None = None
        for attempt in range(_SCRATCH_INIT_ATTEMPTS):
            try:
                if self.scratch.is_dir():
                    ready = self._git(
                        ["rev-parse", "--is-bare-repository"],
                        check=False,
                    )
                    if ready.returncode == 0 and ready.stdout.strip() == "true":
                        return
                self.scratch.mkdir(parents=True, exist_ok=True)
            except (OSError, ClaimTransportError) as exc:
                last_error = exc
                break
            try:
                self._git(["init", "--bare", "--quiet", "."])
                return
            except ClaimTransportError as exc:
                last_error = exc
            if attempt + 1 < _SCRATCH_INIT_ATTEMPTS:
                time.sleep(_SCRATCH_INIT_DELAY_S)
        raise ClaimTransportError(
            "cannot prepare the local claim-board scratch directory"
        ) from last_error

    @staticmethod
    def _ref(key: str) -> str:
        return CLAIM_REF_PREFIX + _validate_key(key)

    def _remote_oid(self, key: str) -> str | None:
        ref = self._ref(key)
        proc = self._git(["ls-remote", self.repo_url, ref])
        for line in proc.stdout.splitlines():
            oid, _, name = line.partition("\t")
            if name == ref:
                return oid
        return None

    def _remote_oids(self, keys: Sequence[str]) -> dict[str, str]:
        """Read every present key's object ID with one ``ls-remote``."""
        keys_by_ref = {self._ref(key): key for key in keys}
        proc = self._git(["ls-remote", self.repo_url, *keys_by_ref])
        oids: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            oid, separator, ref = line.partition("\t")
            # ls-remote patterns also match longer names ending in the same
            # path, such as refs/heads/<ref>; only the exact ref is the claim.
            if separator and ref in keys_by_ref:
                oids[keys_by_ref[ref]] = oid
        return oids

    def _read_lease(self, key: str, oid: str) -> dict[str, Any]:
        ref = self._ref(key)
        if self._git(["cat-file", "-e", f"{oid}^{{commit}}"], check=False).returncode != 0:
            self._git(["fetch", "--quiet", self.repo_url, f"+{ref}:{ref}"])
        proc = self._git(["cat-file", "commit", oid], check=False)
        if proc.returncode != 0:
            raise MalformedLeaseError(f"claim {key!r} does not point to a readable commit")
        _, separator, message = proc.stdout.partition("\n\n")
        if not separator:
            raise MalformedLeaseError(f"claim {key!r} has no lease message")
        try:
            lease = json.loads(
                message,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise MalformedLeaseError(f"claim {key!r} has invalid lease JSON") from exc
        if not isinstance(lease, dict) or not self._lease_is_valid(lease, key):
            raise MalformedLeaseError(f"claim {key!r} has an invalid lease schema")
        return lease

    @staticmethod
    def _lease_is_valid(lease: Mapping[str, Any], key: str | None = None) -> bool:
        acquired_at = lease.get("acquired_at")
        expires_at = lease.get("expires_at")
        valid = (
            lease.get("schema") == CLAIM_SCHEMA
            and isinstance(lease.get("owner"), str)
            and bool(lease.get("owner"))
            and isinstance(lease.get("resource"), str)
            and _is_finite_number(acquired_at)
            and _is_finite_number(expires_at)
            and acquired_at <= expires_at
        )
        return bool(valid and (key is None or lease.get("resource") == key))

    def _held_by_peer(self, key: str, old: str | None) -> bool:
        """Return whether ``old`` is another worker's verified live lease, which blocks acquiring."""
        if old is None:
            return False
        lease = self._read_lease(key, old)
        return bool(
            lease is not None
            and self._lease_is_valid(lease, key)
            and lease.get("owner") != self.worker_id
            and not self.expired(lease)
        )

    def _owned_lease(self, key: str, old: str | None) -> dict[str, Any] | None:
        """Return this worker's verified lease at ``old``, or ``None`` if absent or not owned."""
        if old is None:
            return None
        lease = self._read_lease(key, old)
        if lease is None or not self._lease_is_valid(lease, key) or lease.get("owner") != self.worker_id:
            return None
        return lease

    def _make_lease_commit(self, key: str, ttl: int | float, note: str = "") -> str:
        key = _validate_key(key)
        ttl = _validate_ttl(ttl)
        now = time.time()
        if not math.isfinite(now):
            raise ValueError("claim timestamp must be finite")
        try:
            expires_at = now + ttl
        except OverflowError as exc:
            raise ValueError("claim expiry must be finite") from exc
        if not math.isfinite(expires_at):
            raise ValueError("claim expiry must be finite")
        lease: dict[str, Any] = {
            "schema": CLAIM_SCHEMA,
            "owner": self.worker_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": now,
            "expires_at": expires_at,
            "resource": key,
        }
        if note:
            lease["note"] = note
        tree = self._git(["mktree"], input_text="").stdout.strip()
        message = json.dumps(lease, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._git(["commit-tree", tree, "-m", message]).stdout.strip()

    def _ref_store(self) -> Path | None:
        """Return the Git directory holding the board's refs, or None when its files are not visible.

        A local path, or a ``file://`` URL except on Windows, is visible, found
        as Git's local transport finds it; any other transport is not.
        """
        location = self.repo_url
        if location.startswith("file://"):
            if os.name == "nt":
                # Git for Windows reads drive letters and hosts in a file URL
                # its own way; such a board is treated as not visible.
                return None
            # Git decodes the whole URL, then ignores the host and keeps "?" and "#".
            decoded = os.fsdecode(urllib.parse.unquote_to_bytes(location[len("file://") :]))
            path_start = decoded.find("/")
            if path_start < 0:
                return None
            location = decoded[path_start:]
        elif _repository_is_remote(location):
            return None
        while len(location) > 1 and location.endswith("/"):
            location = location[:-1]
        env = {name: value for name, value in os.environ.items() if name not in _LOCAL_GIT_ENV}
        # Git's enter_repo tries these candidates in this order. It takes the
        # first that is a file or a Git directory, and fails if a file is bad.
        for candidate in (f"{location}/.git", location, f"{location}.git/.git", f"{location}.git"):
            if not os.path.isfile(candidate) and not os.path.isdir(candidate):
                continue
            try:
                proc = subprocess.run(
                    ["git", f"--git-dir={candidate}", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    capture_output=True,
                    timeout=120,
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if proc.returncode == 0:
                return Path(os.fsdecode(proc.stdout.removesuffix(b"\n")))
            if os.path.isfile(candidate):
                return None
        return None

    def ref_locks(self) -> list[ClaimRefLock]:
        """Return the Git lock files that can block the board's claims, once they outlast the grace.

        These are the lock files under the claim refs, ``packed-refs.lock`` and
        a reftable board's ``tables.list.lock``. Git leaves one behind when a
        process stops while updating a ref, and refuses the updates it guards
        until someone deletes it. A younger lock may belong to a push still in
        progress. Git records no owner that could prove a lock abandoned, so
        locks are reported, never deleted. Only a board given as a local path,
        or as a ``file://`` URL except on Windows, has visible files.
        """
        store = self._ref_store()
        if store is None:
            return []
        paths = {store / "packed-refs.lock": "packed-refs", store / "reftable" / "tables.list.lock": "reftable"}
        for directory, _, files in os.walk(store / CLAIM_REF_PREFIX):
            for name in files:
                if name.endswith(".lock"):
                    path = Path(directory) / name
                    paths[path] = path.relative_to(store).as_posix()[: -len(".lock")]
        now = time.time()
        # A modification time ahead of the clock is no sign of a push in progress.
        return [lock for lock in self._existing_locks(paths) if abs(now - lock.modified) >= _LOCK_GRACE_S]

    @staticmethod
    def _existing_locks(paths: Mapping[Path, str]) -> list[ClaimRefLock]:
        locks: list[ClaimRefLock] = []
        for path, ref in paths.items():
            try:
                locks.append(ClaimRefLock(path, ref, path.lstat().st_mtime))
            except OSError:
                continue
        return sorted(locks, key=lambda lock: str(lock.path))

    def _blocking_locks(self, store: Path, updates: Sequence[tuple[str, str | None, str]]) -> list[ClaimRefLock]:
        """Return the lock files that still block a failed push's refs after the grace.

        ``packed-refs.lock`` blocks only deletions; a reftable board's
        ``tables.list.lock`` blocks every update.
        """
        paths = {store / f"{self._ref(key)}.lock": self._ref(key) for key, _, _ in updates}
        if any(not new for _, _, new in updates):
            paths[store / "packed-refs.lock"] = "packed-refs"
        paths[store / "reftable" / "tables.list.lock"] = "reftable"
        deadline = time.monotonic() + _LOCK_GRACE_S
        while True:
            locks = self._existing_locks(paths)
            if not locks or time.monotonic() >= deadline:
                return locks
            time.sleep(_LOCK_POLL_S)

    def _cas_push(self, key: str, old: str | None, new: str) -> bool:
        return self._cas_push_refs([(key, old, new)]).ok

    def _cas_push_refs(
        self,
        updates: Sequence[tuple[str, str | None, str]],
        *,
        atomic: bool = False,
    ) -> ClaimBatchResult:
        """Push ``(key, old, new)`` updates, each leased on its observed ``old``; empty ``new`` deletes.

        With ``atomic`` the remote applies every update or none. A lost
        compare-and-swap is a failed result; anything else raises, including
        ``ClaimRefLockedError`` for a local board's lock file that outlasts the
        grace, which Git's message alone cannot tell apart from a lost race. A
        lock that clears within the grace is a lost race.
        """
        keys_by_ref = {self._ref(key): key for key, _, _ in updates}
        proc = self._git(
            [
                "push",
                "--quiet",
                "--porcelain",
                *(["--atomic"] if atomic else []),
                *(f"--force-with-lease={self._ref(key)}:{old or ''}" for key, old, _ in updates),
                self.repo_url,
                *(f"{new if new else ''}:{self._ref(key)}" for key, _, new in updates),
            ],
            check=False,
        )
        if proc.returncode == 0:
            return ClaimBatchResult(True)
        store = self._ref_store()
        locks = self._blocking_locks(store, updates) if store is not None else []
        if locks:
            raise ClaimRefLockedError(
                f"{'; '.join(lock.describe() for lock in locks)}. Git leaves a lock file behind when a process "
                "stops while updating a ref; if no Git process is using the claim board, "
                f"delete {'them' if len(locks) > 1 else 'it'} and retry",
                locks,
            )
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        if atomic and "does not support --atomic" in detail:
            # Falling back to separate pushes could leave a partial claim set.
            raise ClaimTransportError("claim board does not support atomic pushes; refusing a multi-key update")
        markers = _CAS_REJECTIONS
        # Read only in Git's own refusal lines, never in hook output. With no
        # lock left on a visible board, a lock Git met belonged to a push that
        # has since let go.
        if store is not None or all(old is None for _, old, _ in updates):
            markers += (_LOST_CREATE,)
        stale = _rejected_keys(detail, keys_by_ref, markers)
        if (
            stale
            or any(marker in detail.lower() for marker in _CAS_REJECTIONS)
            or (store is not None and self._names_a_needed_lock(detail, updates))
        ):
            return ClaimBatchResult(False, stale, "changed concurrently" if stale else "a claim changed concurrently")
        raise ClaimTransportError(f"claim CAS push failed: {detail[:300]}")

    def _names_a_needed_lock(self, detail: str, updates: Sequence[tuple[str, str | None, str]]) -> bool:
        """Whether Git could not take a pushed ref's lock, or ``packed-refs.lock`` for a deletion."""
        needed = [f"/{self._ref(key)}" for key, _, _ in updates]
        if any(not new for _, _, new in updates):
            needed.append("/packed-refs")
        return any(path.endswith(tuple(needed)) for path in _LOCK_FILE_EXISTS_RE.findall(detail))

    def read(self, key: str) -> dict[str, Any] | None:
        """Return the current parsed lease, including an expired lease, or ``None``."""
        self._ensure_scratch()
        oid = self._remote_oid(key)
        return self._read_lease(key, oid) if oid else None

    @classmethod
    def expired(cls, lease: Mapping[str, Any], now: float | None = None) -> bool:
        """Return whether a lease is malformed or no longer live."""
        expires_at = lease.get("expires_at")
        if not _is_finite_number(expires_at):
            return True
        comparison_time = time.time() if now is None else now
        if not _is_finite_number(comparison_time):
            raise ValueError("claim expiry comparison clock must be finite")
        return expires_at <= comparison_time

    def acquire(self, key: str, ttl: int | float = CLAIM_TTL_S, steal: bool = False, note: str = "") -> bool:
        """CAS-acquire a free, expired, owned, or explicitly stolen lease.

        A malformed lease raises ``MalformedLeaseError``, even with ``steal``.
        """
        key = _validate_key(key)
        _validate_ttl(ttl)
        self._ensure_scratch()
        old = self._remote_oid(key)
        if self._held_by_peer(key, old) and not steal:
            return False
        new = self._make_lease_commit(key, ttl, note)
        return self._cas_push(key, old, new)

    def renew(self, key: str, ttl: int | float = CLAIM_TTL_S) -> bool:
        """CAS-renew this worker's lease, returning ``False`` if ownership was lost."""
        key = _validate_key(key)
        _validate_ttl(ttl)
        self._ensure_scratch()
        old = self._remote_oid(key)
        lease = self._owned_lease(key, old)
        if lease is None:
            return False
        new = self._make_lease_commit(key, ttl, str(lease.get("note", "")))
        return self._cas_push(key, old, new)

    def release(self, key: str) -> bool:
        """CAS-delete this worker's lease; refuse foreign or unverifiable ownership."""
        key = _validate_key(key)
        self._ensure_scratch()
        old = self._remote_oid(key)
        if old is None:
            return True
        if self._owned_lease(key, old) is None:
            return False
        return self._cas_push(key, old, "")

    # The *_many methods read every key with one ls-remote, apply the per-key
    # checks of the single-key method, and change all keys in one atomic push,
    # so a batch never leaves some of its claims taken and others not. Where
    # the single-key method raises MalformedLeaseError, a batch names that key
    # as blocking instead, so one refusal can explain every blocked key.

    def acquire_many(
        self,
        keys: Sequence[str],
        *,
        ttl: int | float = CLAIM_TTL_S,
        note: str = "",
    ) -> ClaimBatchResult:
        """Acquire every key or none; a live lease of another worker on any key blocks all."""
        keys = _validate_keys(keys)
        _validate_ttl(ttl)
        self._ensure_scratch()
        olds = self._remote_oids(keys)
        blocked: dict[str, str] = {}
        for key in keys:
            try:
                if self._held_by_peer(key, olds.get(key)):
                    blocked[key] = "held by another worker"
            except MalformedLeaseError:
                blocked[key] = "malformed lease"
        if blocked:
            return _refusal(blocked)
        updates = [(key, olds.get(key), self._make_lease_commit(key, ttl, note)) for key in keys]
        return self._cas_push_refs(updates, atomic=True)

    def renew_many(self, keys: Sequence[str], *, ttl: int | float = CLAIM_TTL_S) -> ClaimBatchResult:
        """Renew every key or none; each must be this worker's lease, as :meth:`renew` requires."""
        keys = _validate_keys(keys)
        _validate_ttl(ttl)
        self._ensure_scratch()
        olds = self._remote_oids(keys)
        notes: dict[str, str] = {}
        blocked: dict[str, str] = {}
        for key in keys:
            try:
                lease = self._owned_lease(key, olds.get(key))
            except MalformedLeaseError:
                blocked[key] = "malformed lease"
                continue
            if lease is None:
                blocked[key] = "not held by this worker"
            else:
                notes[key] = str(lease.get("note", ""))
        if blocked:
            return _refusal(blocked)
        updates = [(key, olds[key], self._make_lease_commit(key, ttl, notes[key])) for key in keys]
        return self._cas_push_refs(updates, atomic=True)

    def release_many(self, keys: Sequence[str]) -> ClaimBatchResult:
        """Delete every present key or none; absent keys count as released, as in :meth:`release`."""
        keys = _validate_keys(keys)
        self._ensure_scratch()
        olds = self._remote_oids(keys)
        present = [key for key in keys if key in olds]
        blocked: dict[str, str] = {}
        for key in present:
            try:
                if self._owned_lease(key, olds[key]) is None:
                    blocked[key] = "not held by this worker"
            except MalformedLeaseError:
                blocked[key] = "malformed lease"
        if blocked:
            return _refusal(blocked)
        if not present:
            return ClaimBatchResult(True)
        return self._cas_push_refs([(key, olds[key], "") for key in present], atomic=True)

    def holds(self, key: str) -> bool:
        """Return whether this worker verifiably owns the current live lease."""
        lease = self.read(key)
        return bool(
            lease is not None
            and self._lease_is_valid(lease, key)
            and lease.get("owner") == self.worker_id
            and not self.expired(lease)
        )

    def list(self) -> list[dict[str, Any]]:
        """Return all claim refs, including malformed and expired entries.

        Refs that keep changing while they are read raise ``ClaimTransportError``.
        Any change to a claim ref between an attempt's two ``ls-remote`` calls
        restarts it, so the chance of this error grows with the number of claims
        and with board-wide renew, acquire and release activity.
        """
        self._ensure_scratch()
        for _ in range(_LIST_ATTEMPTS):
            advertised = self._advertised_claims()
            try:
                for key, oid in advertised.items():
                    self._fetch_listed(key, oid)
            except _ClaimRefChanged:
                continue
            if self._advertised_claims() == advertised:
                return self._read_listed(advertised)
        raise ClaimTransportError(f"claim refs changed during each of {_LIST_ATTEMPTS} attempts to list them")

    def _advertised_claims(self) -> dict[str, str]:
        """Return the object ID of every validly named claim ref, by key."""
        proc = self._git(["ls-remote", self.repo_url, CLAIM_REF_PREFIX + "*"])
        advertised: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            oid, separator, ref = line.partition("\t")
            if not separator or not ref.startswith(CLAIM_REF_PREFIX):
                continue
            key = ref[len(CLAIM_REF_PREFIX) :]
            try:
                _validate_key(key)
            except ValueError:
                continue
            advertised[key] = oid
        return advertised

    def _read_listed(self, advertised: Mapping[str, str]) -> list[dict[str, Any]]:
        leases: list[dict[str, Any]] = []
        for key, oid in advertised.items():
            try:
                lease = dict(self._read_lease(key, oid))
            except MalformedLeaseError as exc:
                lease = {
                    "resource": key,
                    "schema": "unreadable",
                    "_error": str(exc),
                    "_malformed": True,
                }
            else:
                lease["_malformed"] = False
            lease["_key"] = key
            lease["_oid"] = oid
            lease["_expired"] = not lease["_malformed"] and self.expired(lease)
            leases.append(lease)
        return sorted(leases, key=lambda lease: str(lease["_key"]))

    def _fetch_listed(self, key: str, oid: str) -> None:
        """Make the listed ``oid`` local, or raise ``_ClaimRefChanged`` if its ref no longer points to it."""
        if self._git(["cat-file", "-e", oid], check=False).returncode == 0:
            return
        ref = self._ref(key)
        fetch = self._git(["fetch", "--quiet", self.repo_url, f"+{ref}:{ref}"], check=False)
        if fetch.returncode == 0 and self._git(["cat-file", "-e", oid], check=False).returncode == 0:
            return
        if fetch.returncode == 0 or self._remote_oid(key) != oid:
            raise _ClaimRefChanged(key)
        raise ClaimTransportError(f"cannot fetch claim {key!r} from the claim board: {fetch.stderr.strip()[:300]}")

    def cleanup(self) -> int:
        """CAS-delete leases expired at snapshot time and return the deletion count.

        A lease whose deletion a lock file blocks stays, as :meth:`ref_locks`
        reports. A lock on ``packed-refs`` or a reftable board blocks every
        deletion, so it ends the cleanup.
        """
        removed = 0
        for lease in self.list():
            if lease["_malformed"] or not lease["_expired"]:
                continue
            try:
                if self._cas_push(str(lease["_key"]), str(lease["_oid"]), ""):
                    removed += 1
            except ClaimRefLockedError as error:
                if any(lock.ref in {"packed-refs", "reftable"} for lock in error.locks):
                    break
        return removed

    def heartbeat(
        self,
        key: str,
        *,
        interval: float = CLAIM_HEARTBEAT_S,
        ttl: int | float = CLAIM_TTL_S,
    ) -> Heartbeat:
        """Create a fail-closed heartbeat for an already acquired lease."""
        return Heartbeat(self, key, interval=interval, ttl=ttl)


class Heartbeat:
    """Renew a lease in a daemon thread and permanently record any uncertainty."""

    def __init__(
        self,
        board: ClaimBoard,
        key: str,
        interval: float = CLAIM_HEARTBEAT_S,
        ttl: int | float = CLAIM_TTL_S,
    ) -> None:
        if not _is_finite_number(interval) or interval <= 0:
            raise ValueError("heartbeat interval must be a finite positive number")
        _validate_key(key)
        _validate_ttl(ttl)
        if interval >= ttl:
            raise ValueError("heartbeat interval must be shorter than the claim TTL")
        self.board = board
        self.key = key
        self.interval = interval
        self.ttl = ttl
        self.lost = threading.Event()
        self.error: Exception | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Heartbeat:
        if self._thread is not None:
            raise RuntimeError("heartbeat cannot be started more than once")
        try:
            renewed = self.board.renew(self.key, ttl=self.ttl)
        except Exception as exc:
            self.error = exc
            self.lost.set()
            raise ClaimTransportError("claim ownership could not be verified before heartbeat entry") from exc
        if not renewed:
            self.lost.set()
            raise ClaimTransportError("claim ownership was lost before heartbeat entry")
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"claim-heartbeat-{self.key}")
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                renewed = self.board.renew(self.key, ttl=self.ttl)
            except Exception as exc:
                self.error = exc
                self.lost.set()
                return
            if not renewed:
                self.lost.set()
                return


__all__ = [
    "CLAIM_HEARTBEAT_S",
    "CLAIM_KEY_RE",
    "CLAIM_REF_PREFIX",
    "CLAIM_SCHEMA",
    "CLAIM_TTL_S",
    "ClaimBatchResult",
    "ClaimBoard",
    "ClaimRefLock",
    "ClaimRefLockedError",
    "ClaimTransportError",
    "Heartbeat",
    "MalformedLeaseError",
    "author_claim_key",
]
