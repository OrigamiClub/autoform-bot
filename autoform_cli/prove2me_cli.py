"""`autoform prove2me` subcommands; the client logic lives in :mod:`.prove2me`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import prove2me
from .prove2me import Prove2MeError


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("prove2me", help="find, mirror, and submit Prove2Me mission theorems")
    commands = parser.add_subparsers(dest="prove2me_command", required=True)

    def command(name: str, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help)
        sub.add_argument(
            "--workspace",
            help="Prove2Me Lean workspace holding credentials.json "
            "(default: $PROVE2ME_WORKSPACE or ~/prove2me_workspace)",
        )
        sub.add_argument("--json", action="store_true", help="write stable machine-readable output")
        return sub

    command("auth", "check the API key and report the platform version; never prints the key")

    mission = command("mission", "show a mission's milestones and open frontier")
    mission.add_argument("mission", help="mission URL, URL slug, name, or id")

    fetch = command("fetch", "mirror a theorem and its platform imports into the workspace")
    fetch.add_argument("theorem_id")

    get = command("get", "read-only GET of any API path, for scouting history, discussion, and submissions")
    get.add_argument("path", help="path under /api/v1, e.g. /theorems/<id>/submissions")
    get.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="query parameter (repeatable)")

    verify = command("verify", "gate, compile, submit, and wait for the verdict")
    verify.add_argument("theorem_id")
    verify.add_argument("solution", type=Path, help="the solution.lean to submit")
    verify.add_argument("--explanation-file", type=Path, help="Markdown explanation to attach")
    verify.add_argument("--disprove", action="store_true", help="the file proves the negation of the statement")
    verify.add_argument("--dry-run", action="store_true", help="run every local check, then stop before submitting")
    verify.add_argument("--skip-compile", action="store_true", help="do not build the file with Lake first")
    verify.add_argument(
        "--allow-env-mismatch",
        action="store_true",
        help="submit even though the workspace pins a different Mathlib than the target",
    )
    verify.add_argument("--no-wait", action="store_true", help="return once the submission is queued")
    verify.add_argument("--timeout", type=float, default=1800, help="seconds to wait for a verdict (default 1800)")
    verify.add_argument("--compile-timeout", type=float, default=1800, help="seconds Lake may build (default 1800)")

    publish = command("publish", "publish a sketch's child lemmas or a definition, then wait for the compile")
    publish.add_argument("kind", choices=("problem", "definition"))
    publish.add_argument("spec", type=Path, help="JSON body: one object, a list, or {\"problems\": [...]}")
    publish.add_argument(
        "--env",
        required=True,
        help="mathlib_rev to publish in; use the parent theorem's, or 'default' for the platform default",
    )
    publish.add_argument("--private", action="store_true", help="visible only to you until made public")
    publish.add_argument("--dry-run", action="store_true", help="validate the spec, then stop before publishing")
    publish.add_argument("--no-wait", action="store_true", help="return once the jobs are queued")
    publish.add_argument("--timeout", type=float, default=900, help="seconds to wait per job (default 900)")

    explain = command("explain", "replace or clear a submission's explanation")
    explain.add_argument("submission_id")
    source = explain.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Markdown explanation")
    source.add_argument("--clear", action="store_true")


def run(args: argparse.Namespace) -> int:
    handler = {
        "auth": _auth,
        "mission": _mission,
        "fetch": _fetch,
        "get": _get,
        "verify": _verify,
        "publish": _publish,
        "explain": _explain,
    }[args.prove2me_command]
    workspace = prove2me.workspace_path(args.workspace)
    try:
        with prove2me.Client(prove2me.load_credentials(workspace)) as client:
            return handler(client, args, workspace)
    except Prove2MeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _emit(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _auth(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    client.authenticate()
    credentials = client.credentials
    expires = credentials.expires_at.isoformat() if credentials.expires_at else None
    if args.json:
        _emit({"source": credentials.source, "username": credentials.username,
                     "expires_at": expires, "platform_version": client.platform_version})
        return 0
    who = f" as {credentials.username}" if credentials.username else ""
    print(f"OK: authenticated{who} with the key from {credentials.source}")
    print(f"    key expires {expires or 'unknown'}; platform version {client.platform_version}")
    return 0


def _mission(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    frontier = prove2me.mission_frontier(client, prove2me.find_mission(client, args.mission))
    if args.json:
        _emit(frontier)
        return 0
    main = frontier["main_theorem"]
    print(f"{frontier['mission']['name']} ({frontier['mission']['id']})")
    print(f"  goal: {main['theorem_name']} [{main['status']}] {main['theorem_id']}")
    print(f"  milestones: {len(frontier['milestones'])}")
    for milestone in frontier["milestones"]:
        theorem = milestone.get("theorem") or {}
        mark = "x" if milestone.get("completed") else " "
        print(f"    [{mark}] {milestone['title']} -> {theorem.get('theorem_name')} {theorem.get('id')}")
    print(f"  open frontier: {len(frontier['open_leaves'])}")
    for leaf in frontier["open_leaves"]:
        print(f"    {leaf['theorem_name']} {leaf['theorem_id']} (closability {leaf.get('closability', 0)})")
    return 0


def _fetch(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    result = prove2me.fetch_theorem(client, args.theorem_id, workspace)
    theorem = result.theorem
    if args.json:
        _emit({
            "theorem_id": theorem.get("theorem_id"),
            "theorem_name": theorem.get("theorem_name"),
            "mathlib_rev": theorem.get("mathlib_rev"),
            "workspace_mathlib_rev": result.workspace_rev,
            "environment_matches": result.environment_matches,
            "written": result.written,
            "unchanged": result.unchanged,
            "unresolved": result.unresolved,
        })
    else:
        print(f"{theorem['theorem_name']} [{theorem.get('status')}] in Mathlib {theorem.get('mathlib_rev')}")
        for path in result.written:
            print(f"  + {path}")
        for path in result.unchanged:
            print(f"  = {path}")
        for module in result.unresolved:
            print(f"warning: could not find platform module {module}")
        if not result.environment_matches:
            print(
                f"warning: workspace pins Mathlib {result.workspace_rev or 'unknown'}, the target needs "
                f"{theorem.get('mathlib_rev')}; re-pin before compiling (Prove2Me references/lean-setup.md)",
                file=sys.stderr,
            )
    return 0 if not result.unresolved else 1


def _get(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    params = {}
    for pair in args.param:
        key, sep, value = pair.partition("=")
        if not sep:
            raise Prove2MeError(f"--param needs KEY=VALUE: {pair!r}")
        params[key] = value
    print(json.dumps(client.get(args.path, **params), indent=None if args.json else 2, ensure_ascii=False))
    return 0


def _verify(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    """Refuse locally whatever the server would reject, then submit.

    A dry run still reads the target for its name and environment, but stops
    before anything is written to the platform.
    """
    path: Path = args.solution
    if not path.is_file():
        raise Prove2MeError(f"no such file: {path}")
    theorem = client.get(f"/theorems/{args.theorem_id}")
    if not isinstance(theorem, dict) or theorem.get("status") == "Definition":
        raise Prove2MeError(f"{args.theorem_id} is not a provable theorem")
    report: dict = {"theorem_id": args.theorem_id, "theorem_name": theorem.get("theorem_name"),
                    "theorem_status": theorem.get("status"), "file": str(path)}

    problems = prove2me.check_solution(path.read_text(encoding="utf-8"), theorem["theorem_name"])
    workspace_rev = prove2me.workspace_mathlib_rev(workspace)
    if workspace_rev != theorem.get("mathlib_rev") and not args.allow_env_mismatch and not args.skip_compile:
        problems.append(
            f"workspace pins Mathlib {workspace_rev or 'unknown'} but the target is verified in "
            f"{theorem.get('mathlib_rev')}; re-pin, or pass --allow-env-mismatch"
        )
    if not problems and not args.skip_compile:
        # Re-mirror so the type check compares against the statement as published now.
        mirrored = prove2me.fetch_theorem(client, args.theorem_id, workspace)
        problems.extend(f"could not mirror platform module {module}" for module in mirrored.unresolved)
    if not problems and not args.skip_compile:
        # Lake prints nothing until it finishes, and a cold `import Mathlib` can take many minutes.
        print(f"building {path.name} and checking it against {theorem['theorem_name']} with Lake; "
              "files that import all of Mathlib can take many minutes", file=sys.stderr, flush=True)
        ok, output = prove2me.compile_locally(path, workspace, theorem["theorem_name"], args.compile_timeout)
        report["compile_output"] = output[-4000:]
        if not ok:
            problems.append("local build or type check failed")
    report["problems"] = problems

    if problems or args.dry_run:
        report["submitted"] = False
        if args.json:
            _emit(report)
        else:
            for problem in problems:
                print(f"error: {problem}")
            if problems and report.get("compile_output"):
                print(report["compile_output"])
            if not problems:
                print(f"OK: {path} passes the local gates for {theorem['theorem_name']}; dry run, nothing submitted")
        return 1 if problems else 0

    explanation = args.explanation_file.read_text(encoding="utf-8") if args.explanation_file else None
    queued = prove2me.submit(
        client,
        args.theorem_id,
        path,
        proof_type="disprove" if args.disprove else "prove",
        explanation=explanation,
    )
    submission_id = queued["submission_id"]
    report.update(submitted=True, submission_id=submission_id, status=queued.get("status"))
    if not args.json:
        print(f"submitted {submission_id} for {theorem['theorem_name']}")
    if not args.no_wait:
        verdict = prove2me.wait_for_verdict(client, submission_id, timeout=args.timeout)
        report.update(status=verdict.get("status"), error_message=verdict.get("error_message") or "")
    if args.json:
        _emit(report)
    else:
        print(f"status: {report['status']}")
        if report.get("error_message"):
            print(report["error_message"])
        if not explanation:
            print("note: no explanation attached; add one with `autoform prove2me explain`")
    if args.no_wait:
        return 0
    return 0 if report["status"] in prove2me.ACCEPTED else 1


def _publish(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Prove2MeError(f"cannot read {args.spec}: {exc}") from None
    items = prove2me.publish_items(args.kind, spec)
    problems = [problem for item in items for problem in prove2me.check_publish(args.kind, item)]
    if problems or args.dry_run:
        for problem in problems:
            print(f"error: {problem}")
        if not problems:
            print(f"OK: {len(items)} {args.kind}(s) pass the local checks; dry run, nothing published")
        return 1 if problems else 0

    env = None if args.env == "default" else args.env
    jobs = prove2me.publish(client, args.kind, items, env, private=args.private)
    if not args.no_wait:
        for job in jobs:
            state = prove2me.wait_for_job(client, job["job_id"], timeout=args.timeout)
            job.update(status=state.get("status"), theorem_id=state.get("theorem_id"),
                       error_message=state.get("error_message") or "")
    if args.json:
        _emit({"jobs": jobs})
    else:
        for job in jobs:
            line = f"{job['name']}: {job.get('status', 'QUEUED')} job {job['job_id']}"
            if job.get("theorem_id"):
                line += f" -> theorem {job['theorem_id']}"
            print(line)
            if job.get("error_message"):
                print(f"  {job['error_message']}")
    if args.no_wait:
        return 0
    return 0 if all(job.get("status") == "PUBLISHED" for job in jobs) else 1


def _explain(client: prove2me.Client, args: argparse.Namespace, workspace: Path) -> int:
    text = None if args.clear else args.file.read_text(encoding="utf-8")
    prove2me.set_explanation(client, args.submission_id, text)
    print(f"OK: explanation {'cleared' if text is None else 'updated'} on {args.submission_id}")
    return 0
