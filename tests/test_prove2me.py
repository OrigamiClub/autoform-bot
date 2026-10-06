from __future__ import annotations

import base64
import datetime
import json
from pathlib import Path

import httpx
import pytest

from autoform_cli import prove2me, prove2me_cli
from autoform_cli.__main__ import main
from autoform_cli.prove2me import Client, Credentials, Prove2MeError


def _key(exp: int) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"p2m_eyJhbGciOiJIUzI1NiJ9.{claims}.c2ln"


FUTURE = int(datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc).timestamp())
KEY = _key(FUTURE)


class FakeApi:
    """A routing table standing in for prove2.me; records every request."""

    def __init__(self, routes: dict[tuple[str, str], object]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.tokens = 0
        self.reject_next_token = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1")
        if path == "/agent/refresh":
            assert json.loads(request.content) == {"api_key": KEY}
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_at": 0, "version": "9.9"})
        if self.reject_next_token:
            self.reject_next_token = False
            return httpx.Response(401, json={"error": "token expired"})
        assert request.headers["Authorization"] == f"Bearer tok{self.tokens}"
        route = self.routes.get((request.method, path))
        if callable(route):
            route = route(request)
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)


def _client(api: FakeApi) -> Client:
    return Client(Credentials(KEY, "test"), transport=httpx.MockTransport(api))


@pytest.fixture
def cli_api(monkeypatch):
    """Route the CLI's client through a FakeApi with the key in the environment."""
    monkeypatch.setenv("PROVE2ME_API_KEY", KEY)
    holder: dict[str, FakeApi] = {}
    real = prove2me.Client

    def make(credentials, **kwargs):
        return real(credentials, transport=httpx.MockTransport(holder["api"]))

    monkeypatch.setattr(prove2me_cli.prove2me, "Client", make)
    return holder


# --- credentials -------------------------------------------------------------


def test_key_expiry_is_read_from_the_key_itself(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PROVE2ME_API_KEY", raising=False)
    (tmp_path / "credentials.json").write_text(json.dumps({"api_key": _key(1000), "username": "u"}))

    credentials = prove2me.load_credentials(tmp_path)

    assert credentials.expires_at == datetime.datetime.fromtimestamp(1000, datetime.timezone.utc)
    assert credentials.expired
    assert KEY not in repr(credentials) and "api_key" not in repr(credentials)
    with pytest.raises(Prove2MeError, match="expired"):
        Client(credentials)


def test_stored_expiry_accepts_unix_seconds_and_iso(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PROVE2ME_API_KEY", raising=False)
    for stored in (FUTURE, "2099-01-01T00:00:00Z"):
        (tmp_path / "credentials.json").write_text(json.dumps({"api_key": "opaque", "api_key_expires_at": stored}))
        assert not prove2me.load_credentials(tmp_path).expired


def test_environment_key_takes_precedence(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "credentials.json").write_text(json.dumps({"api_key": "from-file"}))
    monkeypatch.setenv("PROVE2ME_API_KEY", KEY)

    assert prove2me.load_credentials(tmp_path).source == "PROVE2ME_API_KEY"


@pytest.fixture
def default_workspace(tmp_path: Path, monkeypatch) -> Path:
    """Point the default workspace at an empty directory, never the real one."""
    default = tmp_path / "default-workspace"
    default.mkdir()
    monkeypatch.delenv("PROVE2ME_API_KEY", raising=False)
    monkeypatch.delenv("PROVE2ME_WORKSPACE", raising=False)
    monkeypatch.setattr(prove2me, "DEFAULT_WORKSPACE", default)
    return default


def test_missing_credentials_point_to_onboarding(tmp_path: Path, default_workspace: Path) -> None:
    with pytest.raises(Prove2MeError, match="start.md"):
        prove2me.load_credentials(tmp_path / "checkout")


def test_a_second_checkout_borrows_the_default_workspace_key(tmp_path: Path, default_workspace: Path) -> None:
    (default_workspace / "credentials.json").write_text(json.dumps({"api_key": KEY, "username": "u"}))
    checkout = tmp_path / "prove2me-c5ea003"
    checkout.mkdir()

    credentials = prove2me.load_credentials(checkout)

    assert credentials.source == str(default_workspace / "credentials.json")
    (checkout / "credentials.json").write_text(json.dumps({"api_key": _key(FUTURE + 1)}))
    assert prove2me.load_credentials(checkout).source == str(checkout / "credentials.json")


# --- client ------------------------------------------------------------------


def test_client_reauthenticates_once_on_401_and_only_talks_to_prove2me() -> None:
    api = FakeApi({("GET", "/me"): {"username": "u"}})
    client = _client(api)

    assert client.get("/me") == {"username": "u"}
    api.reject_next_token = True
    assert client.get("/me") == {"username": "u"}
    assert api.tokens == 2
    assert {request.url.host for request in api.requests} == {"prove2.me"}
    for path in ("https://evil.example/me", "//evil.example/me", "me"):
        with pytest.raises(Prove2MeError, match="relative"):
            client.get(path)


def test_errors_carry_the_server_message_but_never_the_key() -> None:
    api = FakeApi({("POST", "/verify"): httpx.Response(429, json={"error": "slow down"})})

    with pytest.raises(Prove2MeError) as error:
        _client(api).request("POST", "/verify", data={"theorem_id": "t"})

    message = str(error.value)
    assert "HTTP 429: slow down" in message and "pending" in message
    assert KEY not in message and "tok1" not in message


def test_revoked_key_is_reported_as_such() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid"})

    client = Client(Credentials(KEY, "test"), transport=httpx.MockTransport(refuse))
    with pytest.raises(Prove2MeError, match="expired or been revoked"):
        client.get("/me")


# --- missions ----------------------------------------------------------------


def _missions(request: httpx.Request) -> dict:
    offset = int(request.url.params["offset"])
    catalogue = [{"id": f"00000000-0000-0000-0000-{i:012d}", "name": f"Mission {i}"} for i in range(150)]
    catalogue[120] = {
        "id": "c36fd4df-ef29-4fbc-9bb6-1f6acf3c0733",
        "name": "Positive definite matrix integral inequality",
        "main_theorem": {"theorem_id": "root", "theorem_name": "P.main", "status": "Open"},
    }
    return {"missions": catalogue[offset : offset + 100], "total": len(catalogue)}


@pytest.mark.parametrize(
    "ref",
    [
        "https://prove2.me/missions/Positive_definite_matrix_integral_inequality/",
        "Positive_definite_matrix_integral_inequality",
        "positive definite matrix integral inequality",
        "C36FD4DF-EF29-4FBC-9BB6-1F6ACF3C0733",
    ],
)
def test_find_mission_pages_the_catalogue(ref: str) -> None:
    api = FakeApi({("GET", "/missions"): _missions})

    assert prove2me.find_mission(_client(api), ref)["name"] == "Positive definite matrix integral inequality"


def test_unknown_mission_is_an_error() -> None:
    with pytest.raises(Prove2MeError, match="no visible"):
        prove2me.find_mission(_client(FakeApi({("GET", "/missions"): _missions})), "Nope")


def test_mission_frontier_ranks_leaves_by_closability() -> None:
    api = FakeApi({
        ("GET", "/missions/m/milestones"): {
            "milestones": [{"id": "ms", "title": "One", "completed": True, "theorem": {"id": "t1"}}],
            "pagination": {"total": 1},
        },
        ("GET", "/theorems/root/open-leaves"): {
            "open_leaves": [
                {"theorem_id": "a", "theorem_name": "a", "closability": 0},
                {"theorem_id": "b", "theorem_name": "b", "closability": 3},
            ],
            "total": 2,
        },
    })
    mission = {"id": "m", "name": "M", "main_theorem": {"theorem_id": "root", "theorem_name": "r", "status": "Open"}}

    frontier = prove2me.mission_frontier(_client(api), mission)

    assert [leaf["theorem_id"] for leaf in frontier["open_leaves"]] == ["b", "a"]
    assert frontier["milestones"][0]["completed"] is True


# --- mirroring ---------------------------------------------------------------


TARGET = {
    "theorem_id": "leaf",
    "theorem_name": "P01.leaf",
    "status": "Open",
    "mathlib_rev": "c5ea",
    "preamble": "import Definitions.Def_outer\nimport Theorems.Thm_P01_helper",
    "formal_statement": "theorem P01.leaf : True := by\n  sorry",
}


def _definitions(request: httpx.Request) -> dict:
    params = request.url.params
    assert params["env"] == "c5ea"
    if params.get("status") == "Definition":
        bodies = {"outer": "import Definitions.Def_inner\ndef outer := inner", "inner": "def inner := 1"}
        name = params["theorem_name"]
        return {"theorems": [{"theorem_name": name, "definition": bodies[name]}] if name in bodies else []}
    if params.get("theorem_name") == "P01_helper":
        return {"theorems": []}
    return {"theorems": [{"theorem_id": "h", "theorem_name": "P01.helper", "status": "Proved"}]}


def test_fetch_mirrors_the_target_and_its_platform_imports(tmp_path: Path) -> None:
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib", "rev": "c5ea"}]}))
    api = FakeApi({
        ("GET", "/theorems/leaf"): TARGET,
        ("GET", "/theorems/h"): {"theorem_name": "P01.helper", "formal_statement": "theorem P01.helper : True := sorry"},
        ("GET", "/theorems"): _definitions,
    })

    result = prove2me.fetch_theorem(_client(api), "leaf", tmp_path)

    assert result.environment_matches and not result.unresolved
    assert sorted(result.written) == [
        "Definitions/Def_inner.lean",
        "Definitions/Def_outer.lean",
        "Theorems/Thm_P01_helper.lean",
        "Theorems/Thm_P01_leaf.lean",
    ]
    assert (tmp_path / "Theorems/Thm_P01_leaf.lean").read_text() == (
        TARGET["preamble"] + "\n\n" + TARGET["formal_statement"] + "\n"
    )
    assert prove2me.fetch_theorem(_client(api), "leaf", tmp_path).written == []


def test_fetch_reports_an_environment_mismatch(tmp_path: Path) -> None:
    (tmp_path / "lakefile.lean").write_text('require mathlib from git\n  "https://x/mathlib4.git" @\n  "777aaa6"\n')
    api = FakeApi({("GET", "/theorems/leaf"): {**TARGET, "preamble": ""}})

    result = prove2me.fetch_theorem(_client(api), "leaf", tmp_path)

    assert result.workspace_rev == "777aaa6"
    assert not result.environment_matches


# --- local gates -------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("theorem solution : True := trivial", []),
        ("-- sorry in a comment\n/- and sorry /- nested -/ here -/\ntheorem solution : True := trivial", []),
        ('theorem solution : "sorry".length = 5 := rfl', []),
        ("namespace X\nend X\nnoncomputable section\ntheorem solution : True := trivial\nend", []),
        ("theorem solution : True := by sorry", ["sorry"]),
        ("theorem solution : True := by admit", ["sorry"]),
        ("import Theorems.Thm_P01_leaf\ntheorem solution : True := trivial", ["own target"]),
        ("namespace P01\ntheorem solution : True := trivial\nend P01", ["inside a namespace"]),
        ("theorem solution2 : True := trivial", ["no top-level"]),
    ],
)
def test_check_solution_enforces_the_platform_gates(source: str, expected: list[str]) -> None:
    problems = prove2me.check_solution(source, "P01.leaf")

    assert len(problems) == len(expected)
    for problem, fragment in zip(problems, expected):
        assert fragment in problem


def test_compile_requires_the_solution_inside_the_workspace(tmp_path: Path) -> None:
    ok, message = prove2me.compile_locally(tmp_path / "elsewhere.lean", tmp_path / "ws", "P01.leaf", 10)

    assert not ok and "Solutions" in message


# --- verdicts ----------------------------------------------------------------


def test_wait_for_verdict_backs_off_until_a_final_status() -> None:
    statuses = iter(["PENDING", "PENDING", "PENDING", "ACCEPTED"])
    api = FakeApi({("GET", "/verify"): lambda request: {"status": next(statuses), "error_message": ""}})
    sleeps: list[float] = []

    verdict = prove2me.wait_for_verdict(_client(api), "s", sleep=sleeps.append, clock=lambda: 0.0)

    assert verdict["status"] == "ACCEPTED"
    assert sleeps == [5.0, 10.0, 20.0]


def test_wait_for_verdict_gives_up_at_the_deadline() -> None:
    api = FakeApi({("GET", "/verify"): {"status": "PENDING"}})
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    verdict = prove2me.wait_for_verdict(_client(api), "s", timeout=12, sleep=sleep, clock=lambda: now[0])

    assert verdict["status"] == "PENDING"
    assert now[0] <= 12


# --- command line ------------------------------------------------------------


def test_auth_command_never_prints_the_key(cli_api, capsys) -> None:
    cli_api["api"] = FakeApi({})

    assert main(["prove2me", "auth"]) == 0
    output = capsys.readouterr()
    assert "platform version 9.9" in output.out
    assert KEY not in output.out + output.err


def test_dry_run_reports_gates_and_never_submits(cli_api, tmp_path: Path, capsys) -> None:
    api = FakeApi({("GET", "/theorems/leaf"): TARGET})
    cli_api["api"] = api
    bad = tmp_path / "bad.lean"
    bad.write_text("theorem solution : True := by sorry\n")
    good = tmp_path / "good.lean"
    good.write_text("theorem solution : True := trivial\n")

    assert main(["prove2me", "verify", "leaf", str(bad), "--dry-run", "--skip-compile", "--workspace", str(tmp_path)]) == 1
    assert "sorry" in capsys.readouterr().out
    assert main(["prove2me", "verify", "leaf", str(good), "--dry-run", "--skip-compile", "--workspace", str(tmp_path)]) == 0
    assert "nothing submitted" in capsys.readouterr().out
    assert not any(request.method != "GET" for request in api.requests if request.url.path != "/api/v1/agent/refresh")


def test_verify_refuses_a_mismatched_environment_before_compiling(cli_api, tmp_path: Path, capsys, monkeypatch) -> None:
    cli_api["api"] = FakeApi({("GET", "/theorems/leaf"): TARGET})
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib", "rev": "777a"}]}))
    good = tmp_path / "good.lean"
    good.write_text("theorem solution : True := trivial\n")
    monkeypatch.setattr(prove2me, "compile_locally", lambda *a, **k: pytest.fail("compiled despite mismatch"))

    assert main(["prove2me", "verify", "leaf", str(good), "--workspace", str(tmp_path)]) == 1
    assert "re-pin" in capsys.readouterr().out


def test_verify_submits_with_explanation_and_waits(cli_api, tmp_path: Path, capsys, monkeypatch) -> None:
    submitted: list[httpx.Request] = []

    def accept(request: httpx.Request) -> dict:
        submitted.append(request)
        return {"submission_id": "sub-1", "status": "PENDING"}

    cli_api["api"] = FakeApi({
        ("GET", "/theorems/leaf"): TARGET,
        ("POST", "/verify"): accept,
        ("GET", "/verify"): {"status": "ACCEPTED", "error_message": ""},
    })
    solution = tmp_path / "Sol.lean"
    solution.write_text("theorem solution : True := trivial\n")
    explanation = tmp_path / "why.md"
    explanation.write_text("We prove $\\top$ directly.")

    code = main([
        "prove2me", "verify", "leaf", str(solution), "--skip-compile",
        "--explanation-file", str(explanation), "--workspace", str(tmp_path), "--json",
    ])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["submission_id"] == "sub-1" and report["status"] == "ACCEPTED"
    body = submitted[0].content
    assert b'name="theorem_id"\r\n\r\nleaf' in body
    assert b'name="proof_type"\r\n\r\nprove' in body
    assert b"We prove $\\top$ directly." in body
    assert b"theorem solution : True := trivial" in body


# --- publishing --------------------------------------------------------------


CHILD = {
    "theorem_name": "P01.child",
    "theorem_title": "Child",
    "formal_statement": "namespace P01\ntheorem child : True := by\n  sorry\nend P01",
    "natural_language_statement": "Truth holds.",
    "preamble": "import Definitions.Def_outer",
}


@pytest.mark.parametrize(
    ("item", "fragment"),
    [
        ({**CHILD, "natural_language_statement": ""}, "missing natural_language_statement"),
        ({**CHILD, "formal_statement": "theorem P01.child : True := trivial"}, "must end with"),
        ({**CHILD, "formal_statement": "theorem other : True := by sorry"}, "does not declare"),
        ({**CHILD, "theorem_title": "x" * 201}, "200 characters"),
        ({**CHILD, "private": True}, "--private"),
    ],
)
def test_check_publish_rejects_malformed_problems(item: dict, fragment: str) -> None:
    assert prove2me.check_publish("problem", CHILD) == []
    assert any(fragment in problem for problem in prove2me.check_publish("problem", item))


def test_check_publish_rejects_sorry_in_definitions() -> None:
    definition = {"definition_name": "helper", "definition_title": "Helper", "definition": "def helper : Nat := sorry"}

    assert any("sorry-free" in problem for problem in prove2me.check_publish("definition", definition))
    assert any("must match" in p for p in prove2me.check_publish("definition", {**definition, "definition_name": "a.b"}))


def test_publish_sends_the_parent_environment_and_waits_for_each_job(
    cli_api, tmp_path: Path, capsys, monkeypatch
) -> None:
    bodies: list[dict] = []
    states = iter(["COMPILING", "PUBLISHED"])

    def queue(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"jobs": [{"job_id": "j1", "name": "P01.child"}], "errors": []})

    cli_api["api"] = FakeApi({
        ("POST", "/submit-problem"): queue,
        ("GET", "/publish-jobs/j1"): lambda request: {"status": next(states), "theorem_id": "new-id"},
    })
    monkeypatch.setattr(prove2me.time, "sleep", lambda seconds: None)
    spec = tmp_path / "child.json"
    spec.write_text(json.dumps({"problems": [CHILD]}))

    code = main(["prove2me", "publish", "problem", str(spec), "--env", "c5ea", "--private",
                 "--workspace", str(tmp_path)])

    assert code == 0
    assert bodies == [{"problems": [CHILD], "env": "c5ea", "private": True}]
    assert "P01.child: PUBLISHED job j1 -> theorem new-id" in capsys.readouterr().out


def test_publish_dry_run_and_invalid_specs_never_post(cli_api, tmp_path: Path, capsys) -> None:
    api = FakeApi({})
    cli_api["api"] = api
    good = tmp_path / "good.json"
    good.write_text(json.dumps(CHILD))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**CHILD, "formal_statement": "theorem P01.child : True := trivial"}))

    assert main(["prove2me", "publish", "problem", str(good), "--env", "c5ea", "--dry-run",
                 "--workspace", str(tmp_path)]) == 0
    assert main(["prove2me", "publish", "problem", str(bad), "--env", "c5ea", "--workspace", str(tmp_path)]) == 1
    assert "must end with" in capsys.readouterr().out
    assert not any(request.method == "POST" and "submit" in request.url.path for request in api.requests)
