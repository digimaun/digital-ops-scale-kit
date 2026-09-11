"""Guards for the Site Ops build, attestation, and qualification workflows.

The trust boundary lives in workflow structure: which job may execute repository
source, which job may sign, which bytes reach qualification and publication, and
which policy values the verification commands pin. These tests read the workflow
documents directly and run the trust-critical shell and Python snippets against
fakes, so a change that widens the boundary fails here.

`on` parses as the boolean True, since YAML 1.1 treats it as a keyword.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.shell_helpers import (
    bash_path as _bash_path,
)
from tests.shell_helpers import (
    required_bash as _required_bash,
)
from tests.shell_helpers import (
    write_executable as _write_executable,
)
from tests.test_distribution_installer import bundle_factory as bundle_factory

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REUSABLE_PATH = WORKFLOWS / "_siteops-distribution.yaml"
CANDIDATE_PATH = WORKFLOWS / "_release-candidate.yaml"
RELEASE_PATH = WORKFLOWS / "release.yaml"
CI_PATH = WORKFLOWS / "ci.yaml"

ON = True
ARCHIVE_NAME = "siteops-install.zip"
ATTESTATION_SUFFIX = ".attestation.jsonl"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
SIGNER_WORKFLOW = ".github/workflows/_siteops-distribution.yaml"
QUALIFIED_PLATFORMS = ("ubuntu-24.04", "windows-2025")
QUALIFIED_PYTHONS = ("3.10", "3.11", "3.12", "3.13", "3.14")

ACTION_PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",
}

VERIFY_FLAGS = (
    "--bundle",
    "--repo",
    "--cert-identity",
    "--signer-digest",
    "--source-digest",
    "--source-ref",
    "--cert-oidc-issuer",
    "--predicate-type",
    "--deny-self-hosted-runners",
)


def _document(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


REUSABLE = _document(REUSABLE_PATH)
CANDIDATE = _document(CANDIDATE_PATH)
RELEASE = _document(RELEASE_PATH)
CI = _document(CI_PATH)


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _script(job: dict, name: str) -> str:
    return _step(job, name)["run"]


def _step_names(job: dict) -> list[str]:
    return [step.get("name", "") for step in job["steps"]]


def _all_steps(document: dict):
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            yield step


def test_ci_rehearsal_requires_an_explicit_manual_request_and_source_commit():
    inputs = CI[ON]["workflow_dispatch"]["inputs"]
    assert inputs["run-mode"]["type"] == "choice"
    assert inputs["run-mode"]["options"] == ["ci-only", "installer-check", "release-preview"]
    assert inputs["run-mode"]["default"] == "ci-only"
    assert inputs["expected-source-sha"]["type"] == "string"
    assert inputs["expected-source-sha"]["required"] is False
    assert inputs["expected-source-sha"]["default"] == ""
    assert inputs["release-file"]["type"] == "string"
    assert inputs["release-file"]["required"] is False
    assert inputs["release-file"]["default"] == (
        ".github/release-examples/combined-preview/release.json"
    )
    job = CI["jobs"]["installer-check"]
    assert job["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && "
        "inputs.run-mode == 'installer-check' }}"
    )
    assert job["uses"] == "./.github/workflows/_siteops-distribution.yaml"
    assert job["with"] == {"expected-source-sha": "${{ inputs.expected-source-sha }}"}
    assert "steps" not in job
    assert "secrets" not in job
    assert job["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
        "attestations": "write",
    }


def test_ci_rehearsal_preserves_normal_ci_permissions_and_cannot_promote():
    assert CI["permissions"] == {"contents": "read"}
    for name in ("lint", "test", "validate"):
        assert CI["jobs"][name]["permissions"] == {"contents": "read"}
    assert all(job.get("permissions", {}).get("contents") != "write" for job in CI["jobs"].values())
    assert "release.yaml" not in yaml.safe_dump(CI["jobs"]["installer-check"])
    release = CI["jobs"]["release-preview"]
    assert release["needs"] == ["lint", "test", "validate"]
    assert release["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.run-mode == 'release-preview' }}"
    )
    assert release["uses"] == "./.github/workflows/_release-candidate.yaml"
    assert release["with"] == {
        "expected-source-sha": "${{ inputs.expected-source-sha }}",
        "intent": "${{ inputs.release-file }}",
        "dry-run": True,
    }
    assert release["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
        "attestations": "write",
    }


def _fake_tools(tmp_path: Path) -> tuple[Path, Path]:
    """Install a recording `gh` and a `python3` shim on a test-owned PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "gh-invocations.log"
    _write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env bash
{
  printf '=== gh ===\\n'
  for argument in "$@"; do printf '%s\\n' "$argument"; done
} >> "$FAKE_GH_LOG"

if [[ "$1" == "attestation" ]]; then
  exit "${FAKE_GH_ATTESTATION_EXIT:-0}"
fi
if [[ "$1" == "api" ]]; then
  if [[ -n "${FAKE_GH_API_RESPONSE:-}" ]]; then
    cat "$FAKE_GH_API_RESPONSE"
  fi
  exit "${FAKE_GH_API_EXIT:-0}"
fi
exit 1
""",
    )
    _write_executable(
        bin_dir / "python3",
        """#!/usr/bin/env bash
exec "$FAKE_PYTHON" "$@"
""",
    )
    return bin_dir, log


def _run_script(script: str, tmp_path: Path, exports: dict[str, str]):
    bin_dir = tmp_path / "bin"
    preamble = []
    if bin_dir.is_dir():
        preamble.append(f'export PATH={shlex.quote(_bash_path(bin_dir))}:"$PATH"')
    preamble.extend(f"export {name}={shlex.quote(value)}" for name, value in exports.items())
    script_path = tmp_path / "workflow-step.sh"
    _write_executable(script_path, "\n".join((*preamble, script)))
    return subprocess.run(
        [
            str(_required_bash()),
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            _bash_path(script_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _invocations(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    calls: list[list[str]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "=== gh ===":
            calls.append([])
        else:
            calls[-1].append(line)
    return calls


# --- Source execution and signing capability stay in separate jobs ------------


def test_build_job_executes_source_without_signing_capability():
    build = REUSABLE["jobs"]["build"]
    assert build["permissions"] == {"contents": "read"}
    assert build["runs-on"] == "ubuntu-24.04"
    assert "environment" not in build
    assert all(step.get("uses", "").split("@")[0] != "actions/attest" for step in build["steps"])


def test_signer_job_holds_signing_capability_without_source_execution():
    attest = REUSABLE["jobs"]["attest"]
    assert attest["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
        "attestations": "write",
    }
    used = {step["uses"].split("@")[0] for step in attest["steps"] if "uses" in step}
    assert used == {
        "actions/download-artifact",
        "actions/attest",
        "actions/upload-artifact",
    }
    for step in attest["steps"]:
        script = step.get("run", "")
        assert "scripts/" not in script
        assert "git " not in script


def test_build_checks_out_the_asserted_event_commit_without_credentials():
    checkout = _step(REUSABLE["jobs"]["build"], "Checkout the event commit")
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "repository": "${{ github.repository }}",
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
        "clean": True,
        "fetch-depth": 1,
    }


def test_expected_source_sha_is_only_an_assertion():
    inputs = REUSABLE[ON]["workflow_call"]["inputs"]
    assert set(inputs) == {"expected-source-sha", "version-mode", "report-summary"}
    assert REUSABLE[ON]["workflow_call"]["inputs"]["version-mode"]["default"] == "build"
    assert inputs["report-summary"] == {
        "description": "Write the aggregate distribution report to the workflow summary.",
        "required": False,
        "type": "boolean",
        "default": True,
    }
    for step in _all_steps(REUSABLE):
        if "uses" in step:
            rendered = yaml.safe_dump(step)
            assert "expected-source-sha" not in rendered
    assertion = _script(REUSABLE["jobs"]["build"], "Assert the expected source commit")
    assert '"$EXPECTED_SOURCE_SHA" != "$SOURCE_SHA"' in assertion
    assert "^[0-9a-f]{40}$" in assertion


def test_build_job_passes_the_event_identity_to_the_producer():
    build = REUSABLE["jobs"]["build"]
    assert build["env"]["SOURCE_SHA"] == "${{ github.sha }}"
    requirements = _script(build, "Install the pinned build requirements")
    assert "--require-hashes" in requirements
    assert "scripts/siteops-build-requirements.txt" in requirements
    script = _script(build, "Build the installation bundle")
    assert "python scripts/build-siteops-bundle.py" in script
    for flag, value in (
        ("--repository", '"$SOURCE_REPOSITORY"'),
        ("--source-ref", '"$SOURCE_REF"'),
        ("--expected-source-sha", '"$SOURCE_SHA"'),
        ("--build-number", '"$BUILD_NUMBER"'),
        ("--build-attempt", '"$BUILD_ATTEMPT"'),
        ("--output", '"$archive"'),
    ):
        assert f"{flag} {value}" in script
    assert "--download-dependencies" in script


# --- The attested bytes are the exact build output ---------------------------


def test_attestation_binds_the_exact_build_artifact_of_this_run():
    attest = REUSABLE["jobs"]["attest"]
    download = _step(attest, "Download the build archive")["with"]
    assert download["artifact-ids"] == "${{ needs.build.outputs.artifact-id }}"
    assert download["run-id"] == "${{ github.run_id }}"
    assert download["repository"] == "${{ github.repository }}"
    assert download["digest-mismatch"] == "error"

    payload = _script(attest, "Confirm the staged payload")
    assert "${#entries[@]} -ne 1" in payload
    assert '"$EXPECTED_ARCHIVE_SHA256"' in payload


def test_attestation_signs_the_literal_archive_with_build_provenance():
    subject = _step(REUSABLE["jobs"]["attest"], "Attest build provenance")
    assert subject["uses"].startswith("actions/attest@")
    assert (
        subject["with"]["subject-path"] == f"${{{{ runner.temp }}}}/siteops-subject/{ARCHIVE_NAME}"
    )
    # Omitting every predicate input selects SLSA build provenance.
    assert set(subject["with"]) == {"subject-path", "show-summary"}
    assert subject["with"]["show-summary"] is False


def test_staging_uploads_never_overwrite_and_fail_on_empty_input():
    uploads = [
        step
        for step in _all_steps(REUSABLE)
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 2
    for upload in uploads:
        assert upload["with"]["overwrite"] is False
        assert upload["with"]["if-no-files-found"] == "error"
        assert "${{ github.run_id }}" in upload["with"]["name"] or (
            upload["with"]["name"].startswith("${{ steps.names.outputs")
        )


def test_every_action_is_pinned_to_the_reviewed_commit():
    for document in (REUSABLE, CANDIDATE, RELEASE, CI):
        for step in _all_steps(document):
            reference = step.get("uses")
            if reference is None or reference.startswith("./"):
                continue
            action, _, sha = reference.partition("@")
            assert action in ACTION_PINS, action
            assert sha == ACTION_PINS[action], action


# --- Qualification verifies the same bytes before touching them --------------


def test_qualification_runs_on_hosted_windows_and_linux_without_write_access():
    qualify = REUSABLE["jobs"]["qualify"]
    assert qualify["strategy"]["matrix"]["os"] == list(QUALIFIED_PLATFORMS)
    assert qualify["strategy"]["matrix"]["python"] == list(QUALIFIED_PYTHONS)
    assert _step(qualify, "Setup Python")["with"]["python-version"] == "${{ matrix.python }}"
    assert qualify["name"] == "Qualify bundle (${{ matrix.os }}, Python ${{ matrix.python }})"
    assert qualify["runs-on"] == "${{ matrix.os }}"
    assert qualify["permissions"] == {"contents": "read", "actions": "read"}
    assert qualify["needs"] == ["build", "attest"]


def test_qualification_verifies_before_it_extracts():
    names = _step_names(REUSABLE["jobs"]["qualify"])
    assert names.index("Verify the bundle before extraction") < names.index(
        "Extract the verified bundle"
    )
    assert names.index("Extract the verified bundle") < names.index(
        "Install and remove Site Ops from the verified bundle"
    )
    assert names.index("Install and remove Site Ops from the verified bundle") < names.index(
        "Confirm the helper result contract"
    )


def test_qualification_policy_pins_the_caller_source_and_local_signer():
    qualify = REUSABLE["jobs"]["qualify"]
    environment = qualify["env"]
    assert environment["SOURCE_REPOSITORY"] == "${{ github.repository }}"
    assert environment["SOURCE_SHA"] == "${{ github.sha }}"
    assert environment["SOURCE_REF"] == "${{ github.ref }}"
    # A local reusable reference binds to the caller's own event commit, so the
    # signer digest is that commit and the identity carries the caller ref.
    assert environment["SIGNER_DIGEST"] == "${{ github.sha }}"
    assert environment["SIGNER_IDENTITY"] == (
        f"https://github.com/${{{{ github.repository }}}}/{SIGNER_WORKFLOW}@${{{{ github.ref }}}}"
    )
    script = _script(qualify, "Verify the bundle before extraction")
    for flag in VERIFY_FLAGS:
        assert flag in script


def test_qualification_requires_the_runner_verifier_capabilities():
    script = _script(
        REUSABLE["jobs"]["qualify"], "Confirm the GitHub CLI verification capabilities"
    )
    assert "command -v gh" in script
    for flag in VERIFY_FLAGS:
        assert flag in script
    assert "curl" not in script and "install" not in script


def test_qualification_isolates_tooling_state_under_the_runner_temporary_path():
    script = _script(
        REUSABLE["jobs"]["qualify"],
        "Install and remove Site Ops from the verified bundle",
    )
    for variable in (
        "PIPX_HOME",
        "PIPX_BIN_DIR",
        "PIPX_MAN_DIR",
        "PIPX_COMPLETION_DIR",
        "PIPX_SHARED_LIBS",
        "PIPX_DEFAULT_PYTHON",
        "PIP_CACHE_DIR",
    ):
        assert f'export {variable}="$' in script
    assert 'owned="$temp/siteops-qualification"' in script
    assert "--output json" in script
    assert '--store-dir "$owned/store"' in script
    assert "--uninstall" in script
    assert '> "$owned/logs/$label.json" 2> "$owned/logs/$label.err"' in script


def test_qualification_checks_the_helper_result_contract():
    script = _script(REUSABLE["jobs"]["qualify"], "Confirm the helper result contract")
    for expected in (
        "siteops.install/v1",
        "SiteOpsInstallationResult",
        '"package") != "siteops"',
        '"installed"',
        '"removed"',
        '"exitCode"',
        '"interrupted"',
        "diagnostic",
    ):
        assert expected in script
    for field in ("sourceRepository", "sourceCommit", "store", "command", "pathReady"):
        assert field in script


def test_the_qualification_pipx_pin_matches_the_repository_pin():
    project = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"pipx=={REUSABLE["env"]["PIPX_VERSION"]}"' in project
    script = _script(REUSABLE["jobs"]["qualify"], "Install the external qualification tooling")
    assert '"pipx==$PIPX_VERSION"' in script
    # The helper expects externally managed tooling, never a bundled copy.
    assert "venv" in script and "install.py" not in script


def test_distribution_summary_uses_only_fixed_outputs_and_job_conclusions():
    summary = REUSABLE["jobs"]["summary"]
    assert summary["needs"] == ["build", "attest", "qualify"]
    assert summary["if"] == "always()"
    assert summary["permissions"] == {"contents": "read", "actions": "read"}
    report = _step(summary, "Aggregate the distribution result")
    assert report["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert report["env"]["REPORT_SUMMARY"] == "${{ inputs.report-summary }}"
    script = report["run"]
    assert (
        "actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT/jobs?per_page=100"
        in script
    )
    assert 'cell = expected.get(job["name"].rsplit(" / ", 1)[-1])' in script
    assert "logs/" not in script
    assert "stdout" not in script
    assert "stderr" not in script
    assert REUSABLE["env"]["SITEOPS_REDACT_OUTPUT"] == "1"


def test_package_downloads_use_the_configured_feed_without_a_public_fallback():
    feed = REUSABLE["env"]["PACKAGE_INDEX_URL"]
    assert feed.startswith("https://")
    assert "pypi.org" not in feed
    for job, step in (
        ("build", "Install the pinned build requirements"),
        ("build", "Build the installation bundle"),
        ("qualify", "Install the external qualification tooling"),
    ):
        environment = _step(REUSABLE["jobs"][job], step)["env"]
        assert environment["PIP_INDEX_URL"] == "${{ env.PACKAGE_INDEX_URL }}"
        assert environment["PIP_KEYRING_PROVIDER"] == "disabled"
        assert environment["PIP_NO_INPUT"] == "1"
        assert environment["PIP_EXTRA_INDEX_URL"] == ""
        if job == "build":
            assert environment["PIP_CONFIG_FILE"] == "/dev/null"
        else:
            assert "export PIP_CONFIG_FILE=" in _script(REUSABLE["jobs"][job], step)


def test_the_signer_identity_confirmation_is_recorded():
    header = REUSABLE_PATH.read_text(encoding="utf-8").split("name: _Site Ops")[0]
    assert SIGNER_WORKFLOW in header
    assert "subject alternative name" in header
    assert "before any public release" in header


def test_distribution_never_reaches_azure_or_a_cluster():
    for path in (REUSABLE_PATH, CANDIDATE_PATH, RELEASE_PATH):
        text = path.read_text(encoding="utf-8")
        for command in ("az ", "kubectl", "azure/login", "AZURE_CLIENT_ID"):
            assert command not in text, path.name


def test_reusable_owns_the_only_distribution_summary():
    assert sum(
        "GITHUB_STEP_SUMMARY" in step.get("run", "")
        for step in _all_steps(REUSABLE)
    ) == 1
    assert "Publish the qualification summary" not in _step_names(REUSABLE["jobs"]["qualify"])
    assert "Record the build identity" not in _step_names(REUSABLE["jobs"]["attest"])


def test_distribution_outputs_include_artifact_url_and_qualification_matrix():
    outputs = REUSABLE[ON]["workflow_call"]["outputs"]
    assert set(outputs) == {
        "package-version",
        "build-number",
        "build-attempt",
        "archive-name",
        "archive-sha256",
        "staging-artifact-id",
        "staging-artifact-name",
        "staging-artifact-url",
        "qualification-matrix",
    }
    attest = REUSABLE["jobs"]["attest"]
    assert attest["outputs"]["staging-artifact-url"] == "${{ steps.stage.outputs.artifact-url }}"
    assert outputs["staging-artifact-url"]["value"] == (
        "${{ jobs.attest.outputs.staging-artifact-url }}"
    )
    assert outputs["qualification-matrix"]["value"] == (
        "${{ jobs.summary.outputs.qualification-matrix }}"
    )


# --- Verification identity stays consistent ---------------------------------


def test_verification_never_falls_back_to_a_pattern_identity():
    # gh marks --cert-identity, --cert-identity-regex, --signer-repo, and
    # --signer-workflow mutually exclusive, and the exact identity is the
    # strongest of the four, so it is the only signer identity flag used.
    for path in (REUSABLE_PATH, RELEASE_PATH, REPO_ROOT / "docs" / "install-siteops.md"):
        text = path.read_text(encoding="utf-8")
        assert "--cert-identity-regex" not in text
        assert "--signer-workflow" not in text
        assert "--signer-repo" not in text
        assert "--cert-identity" in text


def test_the_archive_name_is_the_same_literal_everywhere():
    for path in (REUSABLE_PATH, RELEASE_PATH):
        document = _document(path)
        assert document["env"]["ARCHIVE_NAME"] == ARCHIVE_NAME
        assert document["env"]["ATTESTATION_SUFFIX"] == ATTESTATION_SUFFIX
        assert document["env"]["OIDC_ISSUER"] == OIDC_ISSUER
        assert document["env"]["PREDICATE_TYPE"] == PREDICATE_TYPE


# --- Extracted snippets, exercised against fakes -----------------------------


def _qualification_jobs() -> list[dict]:
    return [
        {
            "name": f"Qualify bundle ({platform}, Python {python})",
            "status": "completed",
            "conclusion": "success",
        }
        for python in QUALIFIED_PYTHONS
        for platform in QUALIFIED_PLATFORMS
    ]


def _run_distribution_summary(
    tmp_path: Path,
    jobs: list[dict],
    *,
    report_summary: bool = True,
    build_result: str = "success",
    attest_result: str = "success",
    qualify_result: str = "success",
):
    _, log = _fake_tools(tmp_path)
    response = tmp_path / "jobs.json"
    response.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    output = tmp_path / "github-output.txt"
    summary = tmp_path / "github-summary.md"
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    artifact_available = attest_result == "success"
    result = _run_script(
        _script(REUSABLE["jobs"]["summary"], "Aggregate the distribution result"),
        tmp_path,
        {
            "FAKE_GH_LOG": _bash_path(log),
            "FAKE_GH_API_RESPONSE": _bash_path(response),
            "FAKE_PYTHON": _python_executable_path(),
            "GITHUB_REPOSITORY": "example/publisher",
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_OUTPUT": _bash_path(output),
            "GITHUB_STEP_SUMMARY": _bash_path(summary),
            "REPORT_SUMMARY": str(report_summary).lower(),
            "BUILD_RESULT": build_result,
            "ATTEST_RESULT": attest_result,
            "QUALIFY_RESULT": qualify_result,
            "PACKAGE_VERSION": "1.0.0b1+build.42.2.gcccccccccccc",
            "ARCHIVE_NAME_VALUE": ARCHIVE_NAME,
            "ARCHIVE_SHA256": "d" * 64,
            "STAGING_ARTIFACT_ID": "987" if artifact_available else "",
            "STAGING_ARTIFACT_NAME": (
                "siteops-install-staging-42-2" if artifact_available else ""
            ),
            "STAGING_ARTIFACT_URL": (
                "https://github.com/example/publisher/actions/runs/42/artifacts/987"
                if artifact_available
                else ""
            ),
            "SOURCE_SHA": "c" * 40,
            "SOURCE_REF": "refs/heads/main",
            "RUNNER_TEMP": _bash_path(runner_temp),
        },
    )
    encoded = None
    if output.exists():
        encoded = output.read_text(encoding="utf-8").split("=", 1)[1].strip()
    return result, encoded, summary, log


def test_distribution_summary_reports_the_complete_success_matrix(tmp_path):
    result, encoded, summary_path, log = _run_distribution_summary(
        tmp_path, _qualification_jobs()
    )

    expected = [
        {"python": python, "linux": "passed", "windows": "passed"}
        for python in QUALIFIED_PYTHONS
    ]
    assert result.returncode == 0, result.stdout + result.stderr
    assert encoded == json.dumps(expected, separators=(",", ":"))
    assert json.loads(encoded) == expected
    assert _invocations(log) == [
        [
            "api",
            "repos/example/publisher/actions/runs/42/attempts/2/jobs?per_page=100",
        ]
    ]

    summary = summary_path.read_text(encoding="utf-8")
    assert summary.count("## Site Ops installer check") == 1
    assert "Version: <code>1.0.0b1+build.42.2.gcccccccccccc</code>" in summary
    assert (
        "[Download the attested installation bundle]"
        "(https://github.com/example/publisher/actions/runs/42/artifacts/987)"
        in summary
    )
    for python in QUALIFIED_PYTHONS:
        assert f"| {python} | passed | passed |" in summary
    for value in (
        "Source SHA",
        "Source ref",
        "Archive SHA-256",
        "Run attempt",
        "Artifact ID",
        "Artifact name",
    ):
        assert value in summary


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("failed", "failed"),
        ("missing", "not-run"),
        ("cancelled", "cancelled"),
        ("ambiguous", "unknown"),
        ("partial", "unknown"),
    ],
)
def test_distribution_summary_reports_nonpassing_cells_honestly(tmp_path, case, expected):
    jobs = _qualification_jobs()
    target = "Qualify bundle (ubuntu-24.04, Python 3.12)"
    selected = next(job for job in jobs if job["name"] == target)
    if case == "failed":
        selected["conclusion"] = "failure"
    elif case == "missing":
        jobs.remove(selected)
    elif case == "cancelled":
        selected["conclusion"] = "cancelled"
    elif case == "ambiguous":
        jobs.append(
            {
                "name": "Qualify bundle (ubuntu-24.04, Python 3.12)",
                "status": "completed",
                "conclusion": "success",
            }
        )
    else:
        selected["status"] = "in_progress"
        selected["conclusion"] = None

    result, encoded, summary_path, _ = _run_distribution_summary(
        tmp_path, jobs, qualify_result="failure"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    matrix = json.loads(encoded)
    assert matrix[2] == {"python": "3.12", "linux": expected, "windows": "passed"}
    assert len(matrix) == len(QUALIFIED_PYTHONS)
    summary = summary_path.read_text(encoding="utf-8")
    assert f"| 3.12 | {expected} | passed |" in summary
    assert "One or more qualification cells did not pass." in summary


@pytest.mark.parametrize("prefix", ["Rehearse distribution", "Rehearse release / Candidate installation"])
def test_distribution_summary_accepts_real_reusable_workflow_job_names(tmp_path, prefix):
    jobs = _qualification_jobs()
    for job in jobs:
        job["name"] = prefix + " / " + job["name"]
    result, encoded, _, _ = _run_distribution_summary(tmp_path, jobs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(row["linux"] == row["windows"] == "passed" for row in json.loads(encoded))


def test_distribution_summary_refuses_ambiguous_success_claim(tmp_path):
    jobs = _qualification_jobs()
    jobs.append(
        {
            "name": "Distribute / Qualify bundle (ubuntu-24.04, Python 3.10)",
            "status": "completed",
            "conclusion": "failure",
        }
    )

    result, encoded, _, _ = _run_distribution_summary(tmp_path, jobs)

    assert result.returncode != 0
    assert json.loads(encoded)[0] == {
        "python": "3.10",
        "linux": "unknown",
        "windows": "passed",
    }


@pytest.mark.parametrize(
    ("build_result", "attest_result", "guidance"),
    [
        ("failure", "skipped", "Review the Build bundle job"),
        ("success", "failure", "Review the Attest bundle job"),
    ],
)
def test_distribution_summary_keeps_earlier_failure_visible(
    tmp_path, build_result, attest_result, guidance
):
    result, encoded, summary_path, _ = _run_distribution_summary(
        tmp_path,
        [],
        build_result=build_result,
        attest_result=attest_result,
        qualify_result="skipped",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(encoded) == [
        {"python": python, "linux": "not-run", "windows": "not-run"}
        for python in QUALIFIED_PYTHONS
    ]
    summary = summary_path.read_text(encoding="utf-8")
    assert guidance in summary
    assert "not available for download" in summary
    assert REUSABLE["jobs"]["summary"]["if"] == "always()"
    assert "continue-on-error" not in REUSABLE["jobs"]["summary"]


def test_report_summary_false_suppresses_only_presentation(tmp_path):
    summary_path = tmp_path / "github-summary.md"
    summary_path.write_text("existing summary\n", encoding="utf-8")
    result, encoded, returned_summary, log = _run_distribution_summary(
        tmp_path, _qualification_jobs(), report_summary=False
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(encoded) == [
        {"python": python, "linux": "passed", "windows": "passed"}
        for python in QUALIFIED_PYTHONS
    ]
    assert returned_summary.read_text(encoding="utf-8") == "existing summary\n"
    assert _invocations(log)
    assert REUSABLE["jobs"]["summary"]["needs"] == ["build", "attest", "qualify"]
    assert "if" not in REUSABLE["jobs"]["qualify"]


def test_distribution_summary_fails_without_job_conclusions_and_publishes_nothing(tmp_path):
    _, log = _fake_tools(tmp_path)
    output = tmp_path / "github-output.txt"
    summary = tmp_path / "github-summary.md"
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    result = _run_script(
        _script(REUSABLE["jobs"]["summary"], "Aggregate the distribution result"),
        tmp_path,
        {
            "FAKE_GH_LOG": _bash_path(log),
            "FAKE_GH_API_EXIT": "1",
            "FAKE_PYTHON": _python_executable_path(),
            "GITHUB_REPOSITORY": "example/publisher",
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_OUTPUT": _bash_path(output),
            "GITHUB_STEP_SUMMARY": _bash_path(summary),
            "REPORT_SUMMARY": "true",
            "BUILD_RESULT": "success",
            "ATTEST_RESULT": "success",
            "QUALIFY_RESULT": "success",
            "PACKAGE_VERSION": "1.0.0b1",
            "ARCHIVE_NAME_VALUE": ARCHIVE_NAME,
            "ARCHIVE_SHA256": "d" * 64,
            "STAGING_ARTIFACT_ID": "987",
            "STAGING_ARTIFACT_NAME": "siteops-install-staging-42-2",
            "STAGING_ARTIFACT_URL": (
                "https://github.com/example/publisher/actions/runs/42/artifacts/987"
            ),
            "SOURCE_SHA": "c" * 40,
            "SOURCE_REF": "refs/heads/main",
            "RUNNER_TEMP": _bash_path(runner_temp),
        },
    )

    assert result.returncode != 0
    assert json.loads(output.read_text(encoding="utf-8").split("=", 1)[1]) == [
        {"python": python, "linux": "unknown", "windows": "unknown"}
        for python in QUALIFIED_PYTHONS
    ]
    assert not summary.exists()


@pytest.mark.parametrize(
    ("expected", "actual", "code"),
    [
        ("a" * 40, "a" * 40, 0),
        ("a" * 40, "b" * 40, 1),
        ("A" * 40, "A" * 40, 1),
        ("abc", "abc", 1),
        ("", "a" * 40, 1),
    ],
)
def test_source_assertion_accepts_only_the_matching_event_commit(tmp_path, expected, actual, code):
    script = _script(REUSABLE["jobs"]["build"], "Assert the expected source commit")
    result = _run_script(
        script,
        tmp_path,
        {"EXPECTED_SOURCE_SHA": expected, "SOURCE_SHA": actual, "VERSION_MODE": "build"},
    )
    assert result.returncode == code, result.stdout + result.stderr


def _qualification_exports(tmp_path: Path, log: Path) -> dict[str, str]:
    return {
        "FAKE_GH_LOG": _bash_path(log),
        "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
        "ARCHIVE_NAME": ARCHIVE_NAME,
        "ATTESTATION_SUFFIX": ATTESTATION_SUFFIX,
        "SOURCE_REPOSITORY": "example/publisher",
        "SOURCE_SHA": "c" * 40,
        "SOURCE_REF": "refs/heads/main",
        "SIGNER_DIGEST": "c" * 40,
        "SIGNER_IDENTITY": (
            f"https://github.com/example/publisher/{SIGNER_WORKFLOW}@refs/heads/main"
        ),
        "OIDC_ISSUER": OIDC_ISSUER,
        "PREDICATE_TYPE": PREDICATE_TYPE,
        "VERSION_MODE": "build",
    }


def _staged_download(tmp_path: Path, *, bundle: bool = True) -> Path:
    download = tmp_path / "temp" / "siteops-download"
    download.mkdir(parents=True)
    (download / ARCHIVE_NAME).write_bytes(b"archive bytes")
    if bundle:
        (download / (ARCHIVE_NAME + ATTESTATION_SUFFIX)).write_text("{}\n")
    return download


def test_qualification_verification_passes_the_exact_policy_to_the_runner_cli(tmp_path):
    _, log = _fake_tools(tmp_path)
    download = _staged_download(tmp_path)
    script = _script(REUSABLE["jobs"]["qualify"], "Verify the bundle before extraction")
    result = _run_script(script, tmp_path, _qualification_exports(tmp_path, log))

    assert result.returncode == 0, result.stdout + result.stderr
    archive = _bash_path(download / ARCHIVE_NAME)
    assert _invocations(log) == [
        [
            "attestation",
            "verify",
            archive,
            "--bundle",
            archive + ATTESTATION_SUFFIX,
            "--repo",
            "example/publisher",
            "--cert-identity",
            f"https://github.com/example/publisher/{SIGNER_WORKFLOW}@refs/heads/main",
            "--signer-digest",
            "c" * 40,
            "--source-digest",
            "c" * 40,
            "--source-ref",
            "refs/heads/main",
            "--cert-oidc-issuer",
            OIDC_ISSUER,
            "--predicate-type",
            PREDICATE_TYPE,
            "--deny-self-hosted-runners",
        ]
    ]


def test_qualification_stops_when_verification_fails(tmp_path):
    _, log = _fake_tools(tmp_path)
    _staged_download(tmp_path)
    script = _script(REUSABLE["jobs"]["qualify"], "Verify the bundle before extraction")
    exports = _qualification_exports(tmp_path, log)
    exports["FAKE_GH_ATTESTATION_EXIT"] = "1"
    result = _run_script(script, tmp_path, exports)
    assert result.returncode != 0


def test_qualification_stops_when_the_detached_proof_is_missing(tmp_path):
    _, log = _fake_tools(tmp_path)
    _staged_download(tmp_path, bundle=False)
    script = _script(REUSABLE["jobs"]["qualify"], "Verify the bundle before extraction")
    result = _run_script(script, tmp_path, _qualification_exports(tmp_path, log))
    assert result.returncode != 0
    assert _invocations(log) == []


def test_extraction_targets_an_owned_empty_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "python-invocations.log"
    _write_executable(
        bin_dir / "fake-python",
        """#!/usr/bin/env bash
for argument in "$@"; do printf '%s\\n' "$argument"; done >> "$FAKE_PYTHON_LOG"
mkdir -p "$5"
printf 'extracted\\n' > "$5/bundle.json"
""",
    )
    download = _staged_download(tmp_path)
    stale = tmp_path / "temp" / "siteops-verified"
    stale.mkdir()
    (stale / "operator-file").write_text("stale", encoding="utf-8")

    script = _script(REUSABLE["jobs"]["qualify"], "Extract the verified bundle")
    result = _run_script(
        script,
        tmp_path,
        {
            "FAKE_PYTHON_LOG": _bash_path(log),
            "PYTHON": _bash_path(bin_dir / "fake-python"),
            "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
            "ARCHIVE_NAME": ARCHIVE_NAME,
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "zipfile",
        "-e",
        _bash_path(download / ARCHIVE_NAME),
        _bash_path(stale),
    ]
    assert not (stale / "operator-file").exists()


def _bundle_document(**overrides) -> dict:
    document = {
        "apiVersion": "siteops.install/v1",
        "kind": "SiteOpsBundle",
        "package": {
            "name": "siteops",
            "version": "1.0.0b1+build.42.1.gcccccccccccc",
            "baseVersion": "1.0.0b1",
            "wheel": "wheels/siteops-1.0.0b1-py3-none-any.whl",
        },
        "source": {
            "repository": "example/publisher",
            "commit": "c" * 40,
            "ref": "refs/heads/main",
        },
        "build": {"number": 42, "attempt": 1},
        "targets": [],
        "files": [],
    }
    document.update(overrides)
    return document


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({}, 0),
        (
            {
                "source": {
                    "repository": "example/publisher",
                    "commit": "d" * 40,
                    "ref": "refs/heads/main",
                }
            },
            1,
        ),
        (
            {
                "source": {
                    "repository": "attacker/fork",
                    "commit": "c" * 40,
                    "ref": "refs/heads/main",
                }
            },
            1,
        ),
        ({"build": {"number": 43, "attempt": 1}}, 1),
        (
            {
                "package": {
                    "name": "siteops",
                    "version": "1.0.0b1",
                    "baseVersion": "1.0.0b1",
                    "wheel": "wheels/siteops.whl",
                }
            },
            1,
        ),
        ({"kind": "OtherBundle"}, 1),
    ],
)
def test_bundle_consistency_check_matches_this_build(tmp_path, overrides, code):
    verified = tmp_path / "temp" / "siteops-verified"
    verified.mkdir(parents=True)
    (verified / "bundle.json").write_text(
        json.dumps(_bundle_document(**overrides)), encoding="utf-8"
    )
    script = _script(REUSABLE["jobs"]["qualify"], "Confirm the bundle describes this build")
    result = _run_script(
        script,
        tmp_path,
        {
            "PYTHON": _bash_path(Path(sys.executable)),
            "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
            "SOURCE_REPOSITORY": "example/publisher",
            "SOURCE_SHA": "c" * 40,
            "SOURCE_REF": "refs/heads/main",
            "BUILD_NUMBER": "42",
            "BUILD_ATTEMPT": "1",
            "VERSION_MODE": "build",
        },
    )
    assert result.returncode == code, result.stdout + result.stderr


def test_bundle_consistency_accepts_an_independent_source_version(tmp_path):
    verified = tmp_path / "temp" / "siteops-verified"
    verified.mkdir(parents=True)
    document = _bundle_document()
    document["package"]["version"] = document["package"]["baseVersion"] = "1.1.0"
    (verified / "bundle.json").write_text(json.dumps(document), encoding="utf-8")
    result = _run_script(
        _script(REUSABLE["jobs"]["qualify"], "Confirm the bundle describes this build"),
        tmp_path,
        {
            "PYTHON": _python_executable_path(),
            "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
            "SOURCE_REPOSITORY": "example/publisher", "SOURCE_SHA": "c" * 40,
            "SOURCE_REF": "refs/heads/main", "BUILD_NUMBER": "42", "BUILD_ATTEMPT": "1",
            "VERSION_MODE": "source",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _python_executable_path() -> str:
    return _bash_path(Path(sys.executable))


def _helper_result(status: str, overrides: dict) -> dict:
    result = {
        "apiVersion": "siteops.install/v1",
        "kind": "SiteOpsInstallationResult",
        "package": "siteops",
        "status": status,
        "version": "1.0.0b1+build.42.1.gcccccccccccc",
        "exitCode": 0,
        "interrupted": False,
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize(
    ("installed", "removed", "code"),
    [
        ({}, {}, 0),
        ({"status": "failed"}, {}, 1),
        ({"status": "already-installed"}, {}, 1),
        ({}, {"status": "not-installed"}, 1),
        ({"version": "1.0.0b1"}, {}, 1),
        ({"exitCode": 1}, {}, 1),
        ({"exitCode": False}, {}, 1),
        ({"exitCode": 0.0}, {}, 1),
        ({"interrupted": True}, {}, 1),
        ({"kind": "SiteOpsBundle"}, {}, 1),
        ({"apiVersion": "siteops.install/v2"}, {}, 1),
        ({"package": "other"}, {}, 1),
        ({"command": "/home/runner/bin/siteops"}, {}, 1),
        ({"store": "/home/runner/store"}, {}, 1),
        ({"sourceCommit": "c" * 40}, {}, 1),
        ({"pathReady": True}, {}, 1),
        ({"diagnostic": {"code": "pipx-failed", "message": "x"}}, {}, 1),
    ],
)
def test_helper_result_contract_is_enforced(tmp_path, installed, removed, code):
    logs = tmp_path / "temp" / "siteops-qualification" / "logs"
    logs.mkdir(parents=True)
    verified = tmp_path / "temp" / "siteops-verified"
    verified.mkdir(parents=True)
    (verified / "bundle.json").write_text(json.dumps(_bundle_document()), encoding="utf-8")
    (logs / "install.json").write_text(
        json.dumps(_helper_result("installed", installed)), encoding="utf-8"
    )
    (logs / "removal.json").write_text(
        json.dumps(_helper_result("removed", removed)), encoding="utf-8"
    )

    script = _script(REUSABLE["jobs"]["qualify"], "Confirm the helper result contract")
    result = _run_script(
        script,
        tmp_path,
        {
            "PYTHON": _python_executable_path(),
            "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
        },
    )
    assert result.returncode == code, result.stdout + result.stderr


@pytest.mark.skipif(
    sys.platform not in {"win32", "linux"},
    reason="Qualification targets Windows and Linux.",
)
def test_native_workflow_installation_uses_the_runner_paths(
    tmp_path,
    bundle_factory,
):
    root, _ = bundle_factory(81)
    temp = tmp_path / "runner temporary"
    temp.mkdir()
    shutil.copytree(root, temp / "siteops-verified")
    tooling = temp / "siteops-tooling" / ("Scripts" if os.name == "nt" else "bin")
    tooling.mkdir(parents=True)
    pipx_name = "pipx.exe" if os.name == "nt" else "pipx"
    shutil.copy2(Path(sys.executable).parent / pipx_name, tooling / pipx_name)
    result = _run_script(
        _script(
            REUSABLE["jobs"]["qualify"], "Install and remove Site Ops from the verified bundle"
        ),
        tmp_path,
        {
            "RUNNER_TEMP": str(temp),
            "RUNNER_OS": "Windows" if os.name == "nt" else "Linux",
            "PYTHON": str(Path(sys._base_executable)),
            "PYTHONPATH": str(REPO_ROOT),
            "SITEOPS_REDACT_OUTPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for filename, status in (("install.json", "installed"), ("removal.json", "removed")):
        document = json.loads(
            (temp / "siteops-qualification" / "logs" / filename).read_text(encoding="utf-8"),
        )
        assert document["status"] == status


def test_every_run_block_parses_as_bash(tmp_path):
    blocks = []
    for name, document in (
        (REUSABLE_PATH.name, REUSABLE),
        (CANDIDATE_PATH.name, CANDIDATE),
        (RELEASE_PATH.name, RELEASE),
    ):
        for job_id, job in document["jobs"].items():
            for step in job.get("steps", []):
                if "run" in step:
                    blocks.append((f"{name}:{job_id}:{step['name']}", step["run"]))
    assert blocks

    for label, script in blocks:
        path = tmp_path / "block.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        result = subprocess.run(
            [str(_required_bash()), "--noprofile", "--norc", "-n", _bash_path(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, f"{label}: {result.stderr}"
