"""Keep release artifact work on admitted 1ES pools and ordinary checks on public runners."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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
        assert gate["with"]["mode"] == mode
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
