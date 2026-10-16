"""Tests for maestro/runtime_contract.py and ``maestro runtime-contract`` (proposal 141)."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import httpx
import pytest

from maestro import gate_environment, runtime_contract
from maestro.config import EXTERNAL_MCP_PREFIXES, Config
from maestro.gate_capacity import _CgroupMemory
from maestro.runtime_contract import (
    ContractEntry,
    ContractError,
    ContractKind,
    ContractVerdict,
    Finding,
    Observations,
    PackageResolution,
    RequirementContext,
    RuntimeContract,
    evaluate,
    load_contract,
    observe,
    requirement_context,
    requirement_holds,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FAKE_DIR = Path("/fake/maestro-data")
_ALL_KINDS = {
    "binary", "distribution", "writable_path", "readable_fact",
    "identity", "toolbelt", "launcher_package",
}


def _entry(kind: str = "binary", name: str = "git", **kw: Any) -> ContractEntry:
    kw.setdefault("required_when", ("always",))
    kw.setdefault("provision", "install it")
    return ContractEntry(kind=cast(ContractKind, kind), name=name, **kw)


def _make_config() -> Config:
    return Config(memory_dir=_FAKE_DIR, workspace=_FAKE_DIR)


def _obs(**kw: Any) -> Observations:
    base: dict[str, Any] = {
        "found_binaries": frozenset(),
        "resolved_distributions": frozenset(),
        "writable_paths": frozenset(),
        "facts": {},
        "identity": (0, 0, "/root"),
    }
    base.update(kw)
    return Observations(**base)


_ALWAYS = RequirementContext(flags=frozenset(), providers=frozenset(), env="")


def _one(entry: ContractEntry, obs: Observations,
         ctx: RequirementContext = _ALWAYS) -> Finding:
    verdict = evaluate(RuntimeContract(entries=(entry,)), ctx, obs)
    return verdict.findings[0]


# ── load_contract ───────────────────────────────────────────────────


def test_shipped_contract_has_every_kind() -> None:
    contract = load_contract()
    kinds = {e.kind for e in contract.entries}
    assert kinds == _ALL_KINDS


def test_default_path_points_at_repo_deploy_dir() -> None:
    expected = _REPO_ROOT / "deploy" / "runtime_contract.toml"
    assert expected == runtime_contract.DEFAULT_CONTRACT_PATH


def test_load_contract_parses_seeded_file(mem_fs: dict[str, str]) -> None:
    mem_fs["/c.toml"] = (
        '[[entry]]\nkind = "identity"\nname = "m"\nuid = 1\ngid = 2\n'
        'home = "/h"\nrequired_when = ["env:production", "always"]\n'
        'scope = "host"\nprovision = "p"\n'
    )
    contract = load_contract(Path("/c.toml"))
    entry = contract.entries[0]
    assert entry.uid == 1
    assert entry.gid == 2
    assert entry.home == "/h"
    assert entry.scope == "host"
    assert entry.required_when == ("env:production", "always")


def test_load_contract_round_trips_paths_and_used_by(
    mem_fs: dict[str, str],
) -> None:
    mem_fs["/c.toml"] = (
        '[[entry]]\nkind = "readable_fact"\nname = "m"\nreader = "meminfo"\n'
        'paths = ["/proc/meminfo", "/proc/x"]\nrequired_when = "always"\n'
        '[[entry]]\nkind = "writable_path"\nname = "h"\npath = "{home}/.m"\n'
        'used_by = ["maestro/config.py"]\nrequired_when = "always"\n'
    )
    fact, home = load_contract(Path("/c.toml")).entries
    assert fact.paths == ("/proc/meminfo", "/proc/x")
    assert fact.used_by == ()
    assert home.used_by == ("maestro/config.py",)
    assert home.paths == ()


def test_expand_substitutes_home_and_memory_dir() -> None:
    expanded = runtime_contract._expand("{home}:{memory_dir}", _make_config(), "/h")
    assert expanded == f"/h:{_FAKE_DIR}"


def test_load_contract_missing_file_raises_contract_error(
    mem_fs: dict[str, str],
) -> None:
    with pytest.raises(ContractError, match="not found"):
        load_contract(Path("/absent.toml"))


def test_load_contract_empty_file(mem_fs: dict[str, str]) -> None:
    mem_fs["/c.toml"] = ""
    assert load_contract(Path("/c.toml")).entries == ()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('kind = "binary"\nrequired_when = "always"', "missing name"),
        ('kind = "bogus"\nname = "x"\nrequired_when = "always"', "unknown kind"),
        ('kind = "binary"\nname = "x"\nscope = "moon"\nrequired_when = "always"',
         "unknown scope"),
        ('kind = "distribution"\nname = "x"\nrequired_when = "always"',
         "requires submodule"),
        ('kind = "readable_fact"\nname = "x"\nreader = "nope"\n'
         'required_when = "always"', "unknown reader"),
        ('kind = "binary"\nname = "x"\nrequired_when = "sometimes"', "malformed"),
        ('kind = "binary"\nname = "x"\nrequired_when = "env:"', "malformed"),
        ('kind = "binary"\nname = "x"\nrequired_when = "config:no_such_flag"',
         "no boolean Config field"),
        ('kind = "binary"\nname = "x"\nrequired_when = "config:memory_dir"',
         "no boolean Config field"),
        ('kind = "binary"\nname = "x"\nrequired_when = [1]', "not a string"),
        ('kind = "binary"\nname = "x"\nrequired_when = []', "empty required_when"),
        ('kind = "binary"\nname = "x"', "missing required_when"),
        ('kind = "identity"\nname = "m"\nuid = "1002"\ngid = 1\nhome = "/h"\n'
         'required_when = "always"', "uid '1002' is not an integer"),
        ('kind = "identity"\nname = "m"\nuid = 1\ngid = true\nhome = "/h"\n'
         'required_when = "always"', "gid True is not an integer"),
        ('kind = "identity"\nname = "m"\nuid = 1\ngid = 1\nhome = 3\n'
         'required_when = "always"', "home 3 is not a string"),
        ('kind = "binary"\nname = "x"\npaths = ["/proc/x"]\nrequired_when = "always"',
         "paths is only valid on readable_fact"),
        ('kind = "readable_fact"\nname = "x"\nreader = "meminfo"\npaths = "/proc"\n'
         'required_when = "always"', "paths '/proc' is not a list"),
        ('kind = "readable_fact"\nname = "x"\nreader = "meminfo"\npaths = [1]\n'
         'required_when = "always"', "paths item 1 is not a string"),
        ('kind = "readable_fact"\nname = "x"\nreader = "meminfo"\n'
         'paths = ["proc/meminfo"]\nrequired_when = "always"', "is not absolute"),
        ('kind = "binary"\nname = "x"\nused_by = ["maestro/a.py"]\n'
         'required_when = "always"', "used_by is only valid on writable_path"),
        ('kind = "writable_path"\nname = "x"\npath = "/p"\nused_by = [2]\n'
         'required_when = "always"', "used_by item 2 is not a string"),
        ('kind = "writable_path"\nname = "x"\npath = "/p"\nused_by = ["a.py"]\n'
         'required_when = "always"', "not a maestro/...py path"),
        ('kind = "launcher_package"\nname = "p"\nlauncher = "bunx"\n'
         'required_when = "always"', "launcher 'bunx' has no registry"),
        ('kind = "launcher_package"\nname = "p>=1"\nlauncher = "uvx"\n'
         'required_when = "always"', "unparseable uvx package spec"),
        ('kind = "launcher_package"\nname = "p"\nlauncher = ["npx"]\n'
         'required_when = "always"', r"launcher \['npx'\] has no registry"),
    ],
)
def test_load_contract_rejects_malformed_entry(
    mem_fs: dict[str, str], body: str, message: str,
) -> None:
    mem_fs["/c.toml"] = f"[[entry]]\n{body}\n"
    with pytest.raises(ContractError, match=message) as exc:
        load_contract(Path("/c.toml"))
    assert "entry 0" in str(exc.value)


# ── requirement_holds / requirement_context ────────────────────────


def test_requirement_tokens() -> None:
    ctx = RequirementContext(
        flags=frozenset({"context7_mcp_enabled"}),
        providers=frozenset({"cli_paced"}),
        env="production",
    )
    assert requirement_holds(_entry(required_when=("always",)), ctx) is True
    assert requirement_holds(
        _entry(required_when=("config:serena_mcp_enabled",
                              "config:context7_mcp_enabled")), ctx,
    ) is True
    assert requirement_holds(
        _entry(required_when=("config:serena_mcp_enabled",)), ctx,
    ) is False
    assert requirement_holds(_entry(required_when=("provider:cli_paced",)), ctx) is True
    assert requirement_holds(_entry(required_when=("provider:anthropic",)), ctx) is False
    assert requirement_holds(_entry(required_when=("env:production",)), ctx) is True
    assert requirement_holds(_entry(required_when=("env:development",)), ctx) is False


def test_requirement_context_from_production_overlay() -> None:
    """The production overlay turns on Context7, which makes npx required."""
    from maestro import config_settings

    prod = config_settings.build_config_authoritative(_REPO_ROOT, "production")
    base = config_settings.build_config_authoritative(_REPO_ROOT, None)
    npx = next(e for e in load_contract().entries if e.name == "npx")
    assert requirement_holds(npx, requirement_context(prod, "production")) is True
    assert requirement_holds(npx, requirement_context(base, None)) is False


def test_requirement_context_collects_providers_and_env() -> None:
    config = _make_config()
    config.sdk_phase_providers = {"introspect": "cli_paced"}
    ctx = requirement_context(config, None)
    assert ctx.providers == frozenset({config.resolved_sdk_provider(), "cli_paced"})
    assert ctx.env == ""


# ── evaluate ────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["binary", "toolbelt"])
def test_evaluate_path_binaries(kind: str) -> None:
    entry = _entry(kind=kind, name="jq", provision="apt-get install jq")
    assert _one(entry, _obs(found_binaries=frozenset({"jq"}))).status == "ok"
    violated = _one(entry, _obs())
    assert violated.status == "violated"
    assert "apt-get install jq" in violated.detail


def test_evaluate_distribution() -> None:
    entry = _entry(kind="distribution", name="maestro_sdk",
                   submodule="maestro_sdk.mcp_config")
    ok = _one(entry, _obs(resolved_distributions=frozenset({"maestro_sdk"})))
    assert ok.status == "ok"
    violated = _one(entry, _obs())
    assert violated.status == "violated"
    assert "maestro_sdk/maestro_sdk.mcp_config" in violated.detail


def test_evaluate_writable_path() -> None:
    entry = _entry(kind="writable_path", name="mem", path="{memory_dir}")
    assert _one(entry, _obs(writable_paths=frozenset({"{memory_dir}"}))).status == "ok"
    assert _one(entry, _obs()).status == "violated"


def test_evaluate_readable_fact() -> None:
    entry = _entry(kind="readable_fact", name="m", reader="meminfo")
    ok = _one(entry, _obs(facts={"meminfo": 0.5}))
    assert ok.status == "ok"
    assert ok.detail == "meminfo=0.5"
    mem = _entry(kind="readable_fact", name="l", reader="cgroup_memory_limit")
    exhausted = _one(mem, _obs(facts={"cgroup_memory_limit": 0.0}))
    assert exhausted.status == "ok"
    assert exhausted.detail == "cgroup_memory_headroom_mib=0"
    none_reading = _one(entry, _obs(facts={"meminfo": None}))
    assert none_reading.status == "violated"


def test_evaluate_identity() -> None:
    entry = _entry(kind="identity", name="m", uid=1002, gid=1002, home="/home/maestro")
    assert _one(entry, _obs(identity=(1002, 1002, "/home/maestro"))).status == "ok"
    assert _one(entry, _obs(identity=(1003, 1002, "/home/maestro"))).status == "violated"


def test_evaluate_launcher_package_is_unverified() -> None:
    entry = _entry(kind="launcher_package", name="pkg", launcher="npx")
    finding = _one(entry, _obs())
    assert finding.status == "unverified"


def test_evaluate_launcher_package_resolutions() -> None:
    entry = _entry(kind="launcher_package", name="pkg", launcher="npx",
                   provision="reachable npm")
    ok = _one(entry, _obs(packages={"pkg": PackageResolution(True, "pkg resolves")}))
    assert ok.status == "ok"
    assert ok.detail == "pkg resolves"
    bad = _one(entry, _obs(packages={"pkg": PackageResolution(False, "gone")}))
    assert bad.status == "violated"
    assert bad.detail == "gone; provision: reachable npm"
    absent = _one(entry, _obs(packages={}))
    assert absent.status == "unverified"


# ── package resolution ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("launcher", "spec", "url"),
    [
        ("npx", "pkg", "https://registry.npmjs.org/pkg/latest"),
        ("npx", "@s/p", "https://registry.npmjs.org/@s%2Fp/latest"),
        ("npx", "pkg@1.2", "https://registry.npmjs.org/pkg/1.2"),
        ("npx", "@s/p@1", "https://registry.npmjs.org/@s%2Fp/1"),
        ("uvx", "x", "https://pypi.org/pypi/x/json"),
        ("uvx", "serena-agent==1.5.3", "https://pypi.org/pypi/serena-agent/1.5.3/json"),
    ],
)
def test_package_url(launcher: str, spec: str, url: str) -> None:
    assert runtime_contract._package_url(launcher, spec) == url


@pytest.mark.parametrize(
    ("launcher", "spec"),
    [("npx", "Bad Name"), ("npx", "pkg@"), ("uvx", "x>=1"), ("uvx", "x=="),
     ("bunx", "pkg")],
)
def test_package_url_rejects(launcher: str, spec: str) -> None:
    assert runtime_contract._package_url(launcher, spec) is None


def _mock_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_resolve_packages_outcomes() -> None:
    codes = {"/ok/latest": 200, "/missing/latest": 404, "/broken/latest": 500}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/down/latest":
            raise httpx.ConnectError("refused")
        return httpx.Response(codes[request.url.path])

    entries = tuple(
        _entry(kind="launcher_package", name=n, launcher="npx")
        for n in ("ok", "missing", "broken", "down")
    )
    result = runtime_contract._resolve_packages(entries, _mock_client(handler))
    assert result["ok"] == PackageResolution(True, "ok resolves on npm")
    assert result["missing"] == PackageResolution(False, "missing not found on npm")
    assert result["broken"] == PackageResolution(False, "npm answered 500")
    assert result["down"] == PackageResolution(False, "npm unreachable: refused")


_NET_CONTRACT = RuntimeContract(entries=(
    _entry(kind="launcher_package", name="wanted", launcher="npx"),
    _entry(kind="launcher_package", name="off", launcher="npx",
           required_when=("config:context7_mcp_enabled",)),
    _entry(kind="launcher_package", name="hosted", launcher="uvx", scope="host"),
))


def test_observe_offline_makes_no_request() -> None:
    with patch("maestro.runtime_contract.httpx.Client") as client_cls:
        obs = observe(_NET_CONTRACT, _make_config())
    assert obs.packages is None
    client_cls.assert_not_called()


def test_observe_network_resolves_only_required_container_packages() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200)

    obs = observe(_NET_CONTRACT, _make_config(), network=True, context=_ALWAYS,
                  client=_mock_client(handler))
    assert requested == ["/wanted/latest"]
    assert obs.packages == {"wanted": PackageResolution(True, "wanted resolves on npm")}


def test_observe_network_builds_context_and_own_client() -> None:
    own = _mock_client(lambda request: httpx.Response(404))
    with patch("maestro.runtime_contract.httpx.Client", return_value=own) as cls, \
         patch("maestro.runtime_contract.requirement_context",
               return_value=_ALWAYS) as ctx:
        obs = observe(_NET_CONTRACT, _make_config(), network=True)
    cls.assert_called_once_with(timeout=runtime_contract._RESOLVE_TIMEOUT_SECONDS)
    ctx.assert_called_once()
    assert own.is_closed
    assert obs.packages == {
        "wanted": PackageResolution(False, "wanted not found on npm"),
    }


def test_evaluate_not_required_and_host_only() -> None:
    not_required = _entry(required_when=("env:production",))
    assert _one(not_required, _obs()).status == "not-required"
    host = _entry(scope="host")
    assert _one(host, _obs()).status == "host-only"


def test_verdict_ok_only_without_violations() -> None:
    entry = _entry()
    assert ContractVerdict(findings=(Finding(entry, "unverified", ""),)).ok is True
    assert ContractVerdict(findings=(Finding(entry, "violated", ""),)).ok is False


# ── observe ─────────────────────────────────────────────────────────


def test_observe_routes_through_existing_readers() -> None:
    config = _make_config()
    contract = RuntimeContract(entries=(
        _entry(kind="binary", name="git"),
        _entry(kind="toolbelt", name="jq"),
        _entry(kind="distribution", name="pkg", submodule="pkg.sub"),
        _entry(kind="distribution", name="bad", submodule="bad.sub"),
        _entry(kind="writable_path", name="mem", path="{memory_dir}/logs"),
        _entry(kind="writable_path", name="ro", path="/ro/file"),
        _entry(kind="writable_path", name="home", path="{home}/.maestro"),
        _entry(kind="readable_fact", name="a", reader="cgroup_cpu_quota"),
        _entry(kind="readable_fact", name="b", reader="cgroup_memory_limit"),
        _entry(kind="readable_fact", name="c", reader="meminfo"),
        _entry(kind="readable_fact", name="d", reader="docker_root_free"),
        _entry(kind="readable_fact", name="e", reader="load_average"),
        _entry(kind="identity", name="m", uid=1, gid=1, home="/h"),
        _entry(kind="launcher_package", name="p", launcher="npx"),
    ))
    mem_dir = str(config.memory_dir)
    existing = {mem_dir, "/", "/h"}
    accessed: list[str] = []

    def fake_access(path: str, mode: int) -> bool:
        accessed.append(path)
        return path in (mem_dir, "/h")

    with patch("maestro.runtime_contract.shutil.which",
               side_effect=lambda n: "/bin/git" if n == "git" else None), \
         patch.object(gate_environment, "_resolves",
                      side_effect=lambda n, f: n.startswith("pkg")) as resolves, \
         patch("maestro.runtime_contract.os.path.exists",
               side_effect=lambda p: p in existing), \
         patch("maestro.runtime_contract.os.access", side_effect=fake_access), \
         patch("maestro.gate_capacity._read_cgroup_quota_cores", return_value=0) as q, \
         patch("maestro.gate_capacity._read_cgroup_memory",
               return_value=_CgroupMemory(enforced=True, headroom_mib=512)) as m, \
         patch("maestro.host_capacity._read_memory_and_swap",
               return_value=(0.25, 0.1)) as mi, \
         patch("maestro.host_capacity._read_docker_root_free_gib",
               return_value=(10.0, "workspace")) as d, \
         patch("maestro.host_capacity._read_load_per_core", return_value=None) as la, \
         patch("maestro.runtime_contract.os.getuid", return_value=1), \
         patch("maestro.runtime_contract.os.getgid", return_value=1), \
         patch("maestro.runtime_contract.pwd.getpwuid",
               return_value=MagicMock(pw_dir="/h")), \
         patch.dict("maestro.runtime_contract.os.environ", {"HOME": ""}):
        obs = observe(contract, config)

    assert obs.found_binaries == frozenset({"git"})
    assert obs.resolved_distributions == frozenset({"pkg"})
    assert obs.writable_paths == frozenset({"{memory_dir}/logs", "{home}/.maestro"})
    assert accessed == [mem_dir, "/", "/h"]
    assert obs.facts == {
        "cgroup_cpu_quota": None,
        "cgroup_memory_limit": 512.0,
        "meminfo": 0.25,
        "docker_root_free": 10.0,
        "load_average": None,
    }
    assert obs.identity == (1, 1, "/h")
    for reader in (q, m, mi, d, la):
        reader.assert_called_once()
    assert resolves.call_count == 3


def test_fact_readers_cover_enforced_quota_and_unenforced_memory() -> None:
    with patch("maestro.gate_capacity._read_cgroup_quota_cores", return_value=4), \
         patch("maestro.gate_capacity._read_cgroup_memory",
               return_value=_CgroupMemory(enforced=False)):
        assert runtime_contract._cgroup_cpu_quota() == 4.0
        assert runtime_contract._cgroup_memory_headroom() is None


def test_nearest_existing_stops_at_root_when_nothing_exists() -> None:
    with patch("maestro.runtime_contract.os.path.exists", return_value=False):
        assert runtime_contract._nearest_existing("/a/b") == "/"


def _observe_home(home_env: str, pw_dir: str) -> tuple[frozenset[str], list[str]]:
    contract = RuntimeContract(entries=(
        _entry(kind="writable_path", name="home", path="{home}/.maestro"),
    ))
    accessed: list[str] = []

    def fake_access(path: str, mode: int) -> bool:
        accessed.append(path)
        return True

    pw = MagicMock(pw_dir=pw_dir) if pw_dir else None
    with patch("maestro.runtime_contract.os.path.exists", return_value=True), \
         patch("maestro.runtime_contract.os.access", side_effect=fake_access), \
         patch("maestro.runtime_contract.pwd.getpwuid",
               return_value=pw, side_effect=None if pw else KeyError(1)), \
         patch.dict("maestro.runtime_contract.os.environ", {"HOME": home_env}):
        obs = observe(contract, _make_config())
    return obs.writable_paths, accessed


def test_observe_home_prefers_home_env_over_passwd() -> None:
    writable, accessed = _observe_home("/env-home", "/pw-home")
    assert accessed == ["/env-home/.maestro"]
    assert writable == frozenset({"{home}/.maestro"})


def test_observe_without_any_home_leaves_home_entry_unobserved() -> None:
    writable, accessed = _observe_home("", "")
    assert accessed == []
    assert writable == frozenset()


def test_identity_without_passwd_entry() -> None:
    with patch("maestro.runtime_contract.os.getuid", return_value=4242), \
         patch("maestro.runtime_contract.os.getgid", return_value=7), \
         patch("maestro.runtime_contract.pwd.getpwuid", side_effect=KeyError(4242)):
        assert runtime_contract._identity() == (4242, 7, "")


# ── parity pins ─────────────────────────────────────────────────────


def test_distribution_entries_match_gate_environment_siblings() -> None:
    declared = {
        (e.name, e.submodule) for e in load_contract().entries
        if e.kind == "distribution"
    }
    assert declared == set(gate_environment._SIBLINGS)


@pytest.mark.parametrize("prefix", EXTERNAL_MCP_PREFIXES)
def test_mcp_launchers_declared(prefix: str) -> None:
    """Each external MCP server's launcher and package are in the contract."""
    toml = tomllib.loads((_REPO_ROOT / "maestro.toml").read_text(encoding="utf-8"))
    mcp = toml["mcp"]
    command = mcp[f"{prefix}_command"]
    args = mcp[f"{prefix}_args"]
    gate = f"config:{prefix}_mcp_enabled"
    entries = load_contract().entries
    binaries = [e for e in entries if e.kind == "binary" and e.name == command]
    assert len(binaries) == 1
    assert gate in binaries[0].required_when
    packages = [
        e for e in entries
        if e.kind == "launcher_package" and e.name in args and e.launcher == command
    ]
    assert len(packages) == 1
    assert packages[0].required_when == (gate,)


# ── CLI ─────────────────────────────────────────────────────────────


def _run_cli(json_flag: bool, verdict: ContractVerdict,
             capsys: pytest.CaptureFixture[str],
             network: bool = False) -> tuple[int, str, MagicMock]:
    from maestro.cli import cmd_runtime_contract

    args = argparse.Namespace(json=json_flag, network=network)
    with patch("maestro.runtime_contract.observe", return_value=_obs()) as obs, \
         patch("maestro.runtime_contract.evaluate", return_value=verdict):
        rc = cmd_runtime_contract(args, _make_config())
    return rc, capsys.readouterr().out, obs


def test_cmd_runtime_contract_forwards_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    verdict = ContractVerdict(findings=(Finding(_entry(), "ok", "on PATH"),))
    _, _, obs = _run_cli(False, verdict, capsys, network=True)
    assert obs.call_args.kwargs["network"] is True
    assert isinstance(obs.call_args.kwargs["context"], RequirementContext)


def test_parser_accepts_runtime_contract_network() -> None:
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "runtime-contract", "--network", "--json"]), \
         patch("maestro.cli.Config.from_env", return_value=_make_config()), \
         patch("maestro.cli.cmd_runtime_contract", return_value=0) as mock_cmd, \
         pytest.raises(SystemExit):
        main()
    args = mock_cmd.call_args.args[0]
    assert args.network is True
    assert args.json is True


def test_cmd_runtime_contract_ok(capsys: pytest.CaptureFixture[str]) -> None:
    verdict = ContractVerdict(findings=(Finding(_entry(), "ok", "on PATH"),))
    rc, out, _ = _run_cli(False, verdict, capsys)
    assert rc == 0
    assert "Runtime Contract" in out
    assert "on PATH" in out
    assert out.rstrip().endswith("OK")


def test_cmd_runtime_contract_violated(capsys: pytest.CaptureFixture[str]) -> None:
    verdict = ContractVerdict(findings=(Finding(_entry(), "violated", "missing"),))
    rc, out, _ = _run_cli(False, verdict, capsys)
    assert rc == 1
    assert "VIOLATED" in out


def test_cmd_runtime_contract_json(capsys: pytest.CaptureFixture[str]) -> None:
    verdict = ContractVerdict(findings=(Finding(_entry(), "violated", "missing"),))
    rc, out, _ = _run_cli(True, verdict, capsys)
    payload = json.loads(out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["findings"][0]["status"] == "violated"
    assert payload["findings"][0]["entry"]["name"] == "git"


def test_cmd_runtime_contract_missing_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from maestro.cli import cmd_runtime_contract

    with patch("maestro.runtime_contract.load_contract",
               side_effect=ContractError("runtime contract not found at /x")):
        rc = cmd_runtime_contract(
            argparse.Namespace(json=False, network=False), _make_config(),
        )
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_main_dispatches_runtime_contract_before_provider_and_secrets() -> None:
    from maestro.cli import main

    config = _make_config()
    with patch("sys.argv", ["maestro", "runtime-contract"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.cmd_runtime_contract", return_value=1) as mock_cmd, \
         patch("maestro.cli.configure_provider") as mock_provider, \
         patch.object(Config, "check_secrets") as mock_secrets, \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    mock_cmd.assert_called_once()
    mock_provider.assert_not_called()
    mock_secrets.assert_not_called()
