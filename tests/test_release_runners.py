"""Keep release artifact work on admitted 1ES pools and ordinary checks on public runners."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.shell_helpers import run_script

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


ADMISSION = workflow("_release-runner.yaml")
SELECT = ADMISSION["jobs"]["select"]
STEP = SELECT["steps"][0]
ENTRY_GUARD = (
    "inputs.release-pool != '' && inputs.release-pool == vars.SITEOPS_RELEASE_POOL && "
    "(github.event_name == 'workflow_dispatch' || "
    "(github.event_name == 'push' && github.ref == 'refs/heads/main'))"
)


def admit(tmp_path, **changes):
    output = tmp_path / "output"
    output.write_text("existing=value\n")
    environment = {
        "RELEASE_PROVENANCE_READY": "true",
        "RELEASE_POOL": "example-release-pool",
        "RELEASE_MODE": "preview",
        "EXPECTED_SOURCE_SHA": "a" * 40,
        "SOURCE_SHA": "a" * 40,
        "SOURCE_REF": "refs/heads/preview",
        "SOURCE_REPOSITORY": "example/content",
        "CALLER_WORKFLOW": "example/content/.github/workflows/ci.yaml@refs/heads/preview",
        "CALLER_EVENT": "workflow_dispatch",
        "GITHUB_OUTPUT": str(output),
        **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ},
        **changes,
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", STEP["run"]],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=15,
    )
    return result, output.read_text()


@pytest.mark.parametrize("mode,event,ref,pool", [
    ("preview", "workflow_dispatch", "refs/heads/feature/release", "fork-pool"),
    ("preview", "workflow_dispatch", "refs/heads/main", "origin-pool"),
    ("release", "workflow_dispatch", "refs/heads/main", "origin-pool"),
    ("release", "push", "refs/heads/main", "fork-pool"),
])
def test_admission_selects_only_the_configured_pool(tmp_path, mode, event, ref, pool):
    caller = "ci.yaml" if mode == "preview" else "release.yaml"
    result, output = admit(
        tmp_path, RELEASE_MODE=mode, CALLER_EVENT=event, SOURCE_REF=ref, RELEASE_POOL=pool,
        CALLER_WORKFLOW=f"example/content/.github/workflows/{caller}@{ref}",
    )
    assert result.returncode == 0, result.stderr
    assert output == f"existing=value\npool={pool}\n"


@pytest.mark.parametrize("changes,diagnostic", [
    ({"CALLER_EVENT": "pull_request"}, "explicit dispatch"),
    ({"CALLER_EVENT": "pull_request_target"}, "explicit dispatch"),
    ({"CALLER_EVENT": "workflow_run"}, "explicit dispatch"),
    ({"CALLER_EVENT": "push"}, "explicit dispatch"),
    ({"SOURCE_REF": "refs/tags/v1", "CALLER_WORKFLOW": "example/content/.github/workflows/ci.yaml@refs/tags/v1"}, "reviewed branch"),
    ({"EXPECTED_SOURCE_SHA": "b" * 40}, "current source commit"),
    ({"EXPECTED_SOURCE_SHA": "main"}, "current source commit"),
    ({"RELEASE_MODE": "unreviewed"}, "supported CI or Release"),
    ({"CALLER_WORKFLOW": "other/content/.github/workflows/ci.yaml@refs/heads/preview"}, "supported CI or Release"),
    ({"CALLER_WORKFLOW": "example/content/.github/workflows/unreviewed.yaml@refs/heads/preview"}, "supported CI or Release"),
    ({"RELEASE_MODE": "release", "CALLER_WORKFLOW": "example/content/.github/workflows/release.yaml@refs/heads/preview"}, "on main"),
    ({"RELEASE_POOL": ""}, "SITEOPS_RELEASE_POOL"),
    ({"RELEASE_POOL": "pool\npool=another"}, "SITEOPS_RELEASE_POOL"),
    ({"RELEASE_POOL": "pool,other"}, "SITEOPS_RELEASE_POOL"),
    ({"RELEASE_POOL": "$(touch injected)"}, "SITEOPS_RELEASE_POOL"),
    ({"RELEASE_POOL": "a" * 101}, "SITEOPS_RELEASE_POOL"),
])
def test_admission_rejects_before_emitting_a_runner_selection(tmp_path, changes, diagnostic):
    result, output = admit(tmp_path, **changes)
    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert output == "existing=value\n"
    assert not (tmp_path / "injected").exists()


def test_admission_has_no_source_or_privileged_capability():
    assert SELECT["runs-on"] == "ubuntu-24.04"
    assert SELECT["permissions"] == {}
    assert SELECT["timeout-minutes"] == 5
    assert len(SELECT["steps"]) == 1
    assert STEP["shell"] == "python"
    assert STEP["env"]["RELEASE_POOL"] == "${{ vars.SITEOPS_RELEASE_POOL }}"
    assert all("uses" not in step for step in SELECT["steps"])
    assert "RELEASE_POOL" not in ADMISSION[True]["workflow_call"]["inputs"]


@pytest.mark.parametrize("ready", ["false", "", "True", "1"])
def test_pending_provenance_qualification_blocks_before_worker_allocation(tmp_path, ready):
    result, output = admit(tmp_path, RELEASE_PROVENANCE_READY=ready)
    assert result.returncode != 0
    assert "Current provenance verification requires github-hosted runners" in result.stderr
    assert output == "existing=value\n"


def test_production_admission_keeps_the_provenance_gate_closed(tmp_path):
    assert STEP["env"]["RELEASE_PROVENANCE_READY"] == "false"
    result, output = admit(
        tmp_path, RELEASE_PROVENANCE_READY=STEP["env"]["RELEASE_PROVENANCE_READY"],
    )
    assert result.returncode != 0 and "not enabled" in result.stderr
    assert output == "existing=value\n"


@pytest.mark.parametrize("name,secured,public", [
    ("_release-candidate.yaml", {"prepare", "workspace-assets", "engine-input", "review"}, {"workspace-qualify", "workspace-qualified"}),
    ("_siteops-distribution.yaml", {"build", "attest"}, {"qualify", "summary"}),
    ("_workspace-distribution.yaml", {"build", "attest"}, set()),
    ("release.yaml", {"publish"}, set()),
    ("ci.yaml", set(), {"lint", "test", "validate"}),
])
def test_runner_placement_follows_artifact_authority(name, secured, public):
    document = workflow(name)
    selected = {key for key, job in document["jobs"].items() if "runs-on" in job}
    assert selected == secured | public
    for key in secured:
        job = document["jobs"][key]
        labels = job["runs-on"]
        pool = "needs.release-runner.outputs.pool" if name == "release.yaml" else "inputs.release-pool"
        assert labels[:2] == ["self-hosted", "${{ format('1ES.Pool={0}', " + pool + ") }}"]
        assert len(labels) == 3 and labels[2].startswith("${{ format('JobId=siteops-")
        assert "github.run_id" in labels[2] and "github.run_attempt" in labels[2]
        if name == "_workspace-distribution.yaml":
            assert "inputs.slot" in labels[2]
        assert not any("ubuntu-" in label or "windows-" in label for label in labels)
    for key in public:
        assert document["jobs"][key]["runs-on"] in {"ubuntu-latest", "ubuntu-24.04", "${{ matrix.os }}"}
    assert len({document["jobs"][key]["runs-on"][2] for key in secured}) == len(secured)


@pytest.mark.parametrize("name,entry", [
    ("_release-candidate.yaml", "prepare"),
    ("_siteops-distribution.yaml", "build"),
    ("_workspace-distribution.yaml", "build"),
])
def test_direct_reusable_calls_cannot_schedule_untrusted_events(name, entry):
    document = workflow(name)
    assert document[True]["workflow_call"]["inputs"]["release-pool"]["required"] is True
    assert " ".join(document["jobs"][entry]["if"].split()) == ENTRY_GUARD
    for key in ("attest",):
        if key in document["jobs"]:
            assert document["jobs"][key]["needs"] == entry
            assert "always()" not in document["jobs"][key].get("if", "")


def test_every_release_entry_point_requires_runner_admission():
    ci, release, candidate = (workflow(name) for name in ("ci.yaml", "release.yaml", "_release-candidate.yaml"))
    for document, mode in ((ci, "preview"), (release, "release")):
        gate = document["jobs"]["release-runner"]
        assert gate["uses"] == "./.github/workflows/_release-runner.yaml"
        assert gate["with"]["mode"] == (
            "${{ inputs.run-mode == 'runner-check' && 'check' || 'preview' }}"
            if mode == "preview" else mode
        )
        assert gate["permissions"] == {"contents": "read"}
    assert "github.event_name == 'workflow_dispatch'" in ci["jobs"]["release-runner"]["if"]
    assert ci["jobs"]["installer-check"]["needs"] == "release-runner"
    assert "release-runner" in ci["jobs"]["release-preview"]["needs"]
    assert release["jobs"]["candidate"]["needs"] == "release-runner"
    assert release["jobs"]["publish"]["needs"] == ["release-runner", "candidate"]
    for document, key in ((ci, "installer-check"), (ci, "release-preview"), (release, "candidate")):
        assert document["jobs"][key]["with"]["release-pool"] == "${{ needs.release-runner.outputs.pool }}"
    for key in ("distribution", "workspace-build"):
        assert candidate["jobs"][key]["with"]["release-pool"] == "${{ inputs.release-pool }}"


@pytest.mark.parametrize("event,allowed", [("workflow_dispatch", True), ("pull_request", False), ("pull_request_target", False), ("push", False)])
def test_runner_check_is_the_only_nonsigning_gate_exception(tmp_path, event, allowed):
    result, output = admit(
        tmp_path, RELEASE_MODE="check", RELEASE_PROVENANCE_READY="false", CALLER_EVENT=event,
    )
    assert (result.returncode == 0) is allowed, result.stderr
    assert ("pool=" in output) is allowed


def test_runner_check_jobs_have_no_source_or_signing_permissions():
    for key in ("probe", "recheck"):
        job = ADMISSION["jobs"][key]
        assert job["permissions"] == {}
        assert job["if"] == "inputs.mode == 'check'"
        assert job["timeout-minutes"] <= 10
        assert all("uses" not in step for step in job["steps"])
        assert job["runs-on"][:2] == [
            "self-hosted", "${{ format('1ES.Pool={0}', needs.select.outputs.pool) }}",
        ]
    assert ADMISSION["jobs"]["probe"]["needs"] == "select"
    assert ADMISSION["jobs"]["recheck"]["needs"] == ["select", "probe"]
    assert ADMISSION["jobs"]["probe"]["runs-on"][2] != ADMISSION["jobs"]["recheck"]["runs-on"][2]
    ci = workflow("ci.yaml")
    for job in ("installer-check", "release-preview"):
        assert ci["jobs"][job]["if"] == (
            "${{ github.event_name == 'workflow_dispatch' && inputs.run-mode == '" + job + "' }}"
        )


BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def probe_environment(tmp_path, monkeypatch):
    output, summary = tmp_path / "outputs", tmp_path / "summary"
    for key, value in {
        "RUNNER_CLASS": "self-hosted", "RUNNER_TEMP": str(tmp_path),
        "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
        "GH_TOKEN": "PRIVATE_SENTINEL", "AZURE_CLIENT_SECRET": "PRIVATE_SENTINEL",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "platform", "linux")
    read = Path.read_text

    def boot_read(path, *args, **kwargs):
        return BOOT if path == Path("/proc/sys/kernel/random/boot_id") else read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boot_read)
    return output, summary


@pytest.mark.parametrize("fault", [None, "missing", "tool-error", "old-gh", "timeout"])
def test_runner_probe_uses_closed_version_commands_and_private_profiles(probe_environment, tmp_path, monkeypatch, fault):
    output, summary = probe_environment
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name: None if fault == "missing" else name)

    def run(argv, **options):
        expected = {
            "git": (["git", "--version"], "git version 2.50.0\n"),
            "gh": (["gh", "--version"], "gh version 2.94.0\n" if fault == "old-gh" else "gh version 2.101.0\n"),
            "az": (["az", "version", "--query", '"azure-cli"', "--output", "tsv"], "2.90.0\n"),
        }
        assert argv == expected[argv[0]][0]
        assert options["timeout"] == 30 and options["stdin"] == subprocess.DEVNULL
        environment = options["env"]
        assert "GH_TOKEN" not in environment and "AZURE_CLIENT_SECRET" not in environment
        assert Path(environment["HOME"]).is_relative_to(tmp_path)
        assert Path(environment["AZURE_CONFIG_DIR"]).is_relative_to(Path(environment["HOME"]))
        assert Path(environment["GH_CONFIG_DIR"]).is_relative_to(Path(environment["HOME"]))
        assert environment["AZURE_CORE_COLLECT_TELEMETRY"] == "false"
        calls.append(argv)
        if fault == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        return subprocess.CompletedProcess(argv, 7 if fault == "tool-error" else 0, expected[argv[0]][1], "PRIVATE_SENTINEL")

    monkeypatch.setattr(subprocess, "run", run)
    script = ADMISSION["jobs"]["probe"]["steps"][0]["run"]
    if fault is None:
        exec(compile(script, "<runner-probe>", "exec"), {})
        assert len(calls) == 3
        text = summary.read_text()
        assert "2.101.0" in text and "PRIVATE_SENTINEL" not in text and BOOT not in text
        assert output.read_text() == "boot-session=" + hashlib.sha256(("42:1:" + BOOT).encode()).hexdigest() + "\n"
    else:
        with pytest.raises(SystemExit, match="::error::") as failure:
            exec(compile(script, "<runner-probe>", "exec"), {})
        assert "PRIVATE_SENTINEL" not in str(failure.value)
        assert not output.exists() and not summary.exists()


@pytest.mark.parametrize("previous,success", [
    (hashlib.sha256(("42:1:" + BOOT).encode()).hexdigest(), False),
    ("b" * 64, True),
    ("", False),
])
def test_runner_session_comparison_reports_only_the_observed_boundary(probe_environment, monkeypatch, previous, success):
    _, summary = probe_environment
    monkeypatch.setenv("PREVIOUS_BOOT_SESSION", previous)
    script = ADMISSION["jobs"]["recheck"]["steps"][0]["run"]
    if success:
        exec(compile(script, "<runner-recheck>", "exec"), {})
        text = summary.read_text()
        assert "different boot sessions" in text
        assert "does not establish Trusted Launch" in text
        assert BOOT not in text and previous not in text
    else:
        with pytest.raises(SystemExit, match="::error::"):
            exec(compile(script, "<runner-recheck>", "exec"), {})
        assert not summary.exists()


def test_workflow_job_environment_does_not_use_a_direct_runner_context():
    for path in WORKFLOWS.glob("*.yaml"):
        document = workflow(path.name)
        for name, job in document.get("jobs", {}).items():
            for key, value in job.get("env", {}).items():
                assert not re.search(r"\$\{\{\s*runner\.", str(value)), f"{path.name}:{name}:{key}"


@pytest.mark.parametrize("file,job,step,expected", [
    ("_release-candidate.yaml", "workspace-assets", "Initialize private collection home", {
        "HOME": "workspace-verification-home",
    }),
    ("_release-candidate.yaml", "engine-input", "Initialize private engine selection home", {
        "HOME": "engine-selection-home",
    }),
    ("_workspace-distribution.yaml", "build", "Initialize private workspace paths", {
        "HOME": "workspace-home", "AZURE_CONFIG_DIR": "workspace-azure",
        "WORKSPACE_PYTHON": "workspace-tools/bin/python",
    }),
])
def test_private_runtime_paths_are_initialized_before_source_steps(tmp_path, file, job, step, expected):
    selected = workflow(file)["jobs"][job]
    assert selected["steps"][0]["name"] == step
    environment_file = tmp_path / "environment"
    runtime = tmp_path / "runner temp"
    runtime.mkdir()
    result = run_script(
        selected["steps"][0]["run"], tmp_path,
        {"RUNNER_TEMP": runtime.as_posix(), "GITHUB_ENV": environment_file.as_posix()},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("=", 1) for line in environment_file.read_text().splitlines())
    assert values == {key: runtime.as_posix() + "/" + path for key, path in expected.items()}
    assert (runtime / expected["HOME"]).is_dir()
    if "AZURE_CONFIG_DIR" in expected:
        assert (runtime / expected["AZURE_CONFIG_DIR"]).is_dir()
