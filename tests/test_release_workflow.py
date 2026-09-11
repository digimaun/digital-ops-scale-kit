"""Exercise the declaration-driven publisher through its actual workflow steps."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

import pytest
import yaml

from tests.shell_helpers import bash_path, write_executable
from tests.test_distribution_workflows import _run_script
from tests.test_release_intent import CLI, _commit, _write_record, _write_source_version
from tests.test_release_intent import repository as repository

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yaml").read_text())
CANDIDATE_WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "_release-candidate.yaml").read_text())
JOBS = {**CANDIDATE_WORKFLOW["jobs"], **WORKFLOW["jobs"]}
SHA = "c" * 40
REPO = "example/publisher"
ARCHIVE = "siteops-install.zip"
PROOF = ARCHIVE + ".attestation.jsonl"


def step(job, name):
    return next(item for item in JOBS[job]["steps"] if item.get("name") == name)


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def candidate(tmp_path):
    directory = tmp_path / "temp"
    plan_dir = directory / "release-plan"
    plan_dir.mkdir(parents=True)
    bundle_dir = directory / "release-bundle"
    bundle_dir.mkdir()
    declaration = {"tag": "v1.0.0b8", "siteops": {"build": True}}
    raw = json.dumps(declaration).encode()
    notes = b"## Changes\n\nReviewed release notes.\n"
    plan = {
        "apiVersion": "siteops.release/v1", "kind": "ReleaseCandidate", "active": True, "dryRun": False,
        "source": {"repository": REPO, "commit": SHA, "ref": "refs/heads/main"},
        "intent": {
            "path": "releases/candidate/release.json", "sha256": digest(raw),
            "notesPath": "releases/candidate/notes.md", "notesSha256": digest(notes),
        },
        "release": {
            "stream": "scalekit", "tag": "v1.0.0b8", "version": "1.0.0b8",
            "title": "Digital Operations Scale Kit 1.0.0b8", "prerelease": True, "latest": False,
        },
        "siteops": {
            "bundle": True, "versionMode": "build", "baseVersion": "1.0.0b1", "releaseTag": None,
        },
    }
    manifest = {
        "apiVersion": "siteops.install/v1", "kind": "SiteOpsBundle",
        "source": plan["source"], "build": {"number": 42, "attempt": 1},
        "package": {"name": "siteops", "version": "1.0.0b1+build.42.1.gcccccccccccc"},
    }
    with zipfile.ZipFile(bundle_dir / ARCHIVE, "w") as archive:
        archive.writestr("bundle.json", json.dumps(manifest))
    (bundle_dir / PROOF).write_text("synthetic proof")
    (plan_dir / "release-notes.md").write_bytes(notes)
    (plan_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (directory / "release-notes").mkdir()
    (directory / "release-notes" / "publish-notes.md").write_bytes(notes)
    responses = {}
    for revision in (SHA, "refs/heads/main"):
        for name, body in (("release.json", raw.decode()), ("notes.md", notes.decode())):
            responses[f"repos/{REPO}/contents/releases/candidate/{name}?ref={revision}"] = {
                "status": 200, "raw": body,
            }
    return {"root": directory, "plan": plan, "declaration": declaration, "responses": responses}


@pytest.fixture
def runner(tmp_path, candidate):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = tmp_path / "fake-gh.py"
    fake.write_text(
        """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as output:
    output.write(json.dumps(args) + "\\n")
if args[:2] == ["attestation", "verify"]:
    raise SystemExit(int(os.environ.get("FAIL_ATTESTATION", "0")))
if args[0] == "release":
    raise SystemExit(int(os.environ.get("FAIL_RELEASE", "0")))
if args[0] != "api":
    raise SystemExit("Unsupported fake command.")
endpoint = next((value for value in args if value.startswith("repos/")), None)
responses = json.loads(Path(os.environ["FAKE_RESPONSES"]).read_text())
if endpoint not in responses:
    raise SystemExit("Unexpected fake GitHub request: " + str(endpoint))
response = responses[endpoint]
status = response["status"]
if "--include" in args:
    print("HTTP/2.0 " + str(status) + (" Not Found" if status == 404 else " Result"))
    print()
if "raw" in response:
    sys.stdout.buffer.write(response["raw"].encode("utf-8"))
else:
    print(json.dumps(response.get("body", {})))
raise SystemExit(0 if 200 <= status < 300 else 1)
""",
        encoding="utf-8",
    )
    shim = tmp_path / "python-shim.py"
    shim.write_text(
        """import os, subprocess, sys, time
native = subprocess.run
def isolated_run(command, *args, **kwargs):
    if command[0] != "gh":
        raise AssertionError("Unexpected subprocess in workflow fixture.")
    return native([sys.executable, os.environ["FAKE_PROGRAM"], *command[1:]], *args, **kwargs)
subprocess.run = isolated_run
if os.environ.get("FAST_CI_CLOCK"):
    ticks = iter([0, 2000, 4000])
    time.monotonic = lambda: next(ticks)
    time.sleep = lambda _: None
assert sys.argv[1] == "-c"
code = sys.argv[2]
sys.argv = ["-c", *sys.argv[3:]]
exec(compile(code, "<workflow>", "exec"), {"__name__": "__main__"})
""",
        encoding="utf-8",
    )
    write_executable(
        bin_dir / "gh",
        '#!/usr/bin/env bash\nexec "$FAKE_PYTHON" "$FAKE_PROGRAM" "$@"\n',
    )
    write_executable(
        bin_dir / "python3",
        '#!/usr/bin/env bash\nexec "$FAKE_PYTHON" "$FAKE_SHIM" "$@"\n',
    )
    responses = tmp_path / "responses.json"
    calls = tmp_path / "calls.jsonl"
    counter = 0

    def invoke(job, name, *, extra=None):
        nonlocal counter
        counter += 1
        (candidate["root"] / "release-plan" / "plan.json").write_text(
            json.dumps(candidate["plan"]), encoding="utf-8",
        )
        responses.write_text(json.dumps(candidate["responses"]), encoding="utf-8")
        output = tmp_path / f"output-{counter}.txt"
        environment = {
            "FAKE_PYTHON": Path(sys.executable).as_posix(),
            "FAKE_PROGRAM": fake.as_posix(), "FAKE_SHIM": shim.as_posix(),
            "FAKE_RESPONSES": responses.as_posix(), "FAKE_CALLS": calls.as_posix(),
            "GITHUB_OUTPUT": output.as_posix(),
            "GITHUB_STEP_SUMMARY": (tmp_path / "summary.md").as_posix(),
            "GITHUB_REPOSITORY": REPO, "SOURCE_SHA": candidate.get("source_sha", SHA),
            "SOURCE_REF": "refs/heads/main",
            "DRY_RUN": "false",
            "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
            "RUNNER_TEMP": candidate["root"].as_posix(),
            "ARCHIVE_NAME": ARCHIVE, "ATTESTATION_SUFFIX": ".attestation.jsonl",
            "SIGNER_IDENTITY": f"https://github.com/{REPO}/.github/workflows/_siteops-distribution.yaml@refs/heads/main",
            "BUILD_NUMBER": "42", "BUILD_ATTEMPT": "1",
            "OIDC_ISSUER": "https://token.actions.githubusercontent.com",
            "PREDICATE_TYPE": "https://slsa.dev/provenance/v1",
            "BUILD_ARCHIVE_SHA": digest((candidate["root"] / "release-bundle" / ARCHIVE).read_bytes()),
            "APPROVED_PLAN_SHA": digest((candidate["root"] / "release-plan" / "plan.json").read_bytes()),
            "APPROVED_NOTES_SHA": digest((candidate["root"] / "release-notes" / "publish-notes.md").read_bytes()),
            "APPROVED_BUNDLE_SHA": digest((candidate["root"] / "release-bundle" / ARCHIVE).read_bytes()),
            "TAG": candidate["plan"]["release"]["tag"],
        }
        environment.update(extra or {})
        result = _run_script(step(job, name)["run"], tmp_path, environment)
        values = dict(
            line.split("=", 1) for line in output.read_text().splitlines()
        ) if output.exists() else {}
        recorded = [
            json.loads(line) for line in calls.read_text().splitlines()
        ] if calls.exists() else []
        return result, values, recorded

    return invoke


def test_publication_uses_only_the_completed_candidate_and_required_approval():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert "head_sha=" in step("review", "Require successful CI for the candidate")["run"]
    assert JOBS["distribution"]["uses"] == "./.github/workflows/_siteops-distribution.yaml"
    assert JOBS["distribution"]["with"]["version-mode"] == "${{ needs.prepare.outputs.version-mode }}"
    condition = " ".join(JOBS["review"]["if"].split())
    assert "needs.distribution.result == 'success'" in condition
    assert "needs.distribution.result == 'skipped'" in condition
    assert JOBS["publish"]["needs"] == "candidate"
    assert JOBS["publish"]["if"] == "needs.candidate.result == 'success' && needs.candidate.outputs.active == 'true'"
    assert JOBS["candidate"]["uses"] == "./.github/workflows/_release-candidate.yaml"
    assert JOBS["candidate"]["with"]["dry-run"] is False
    assert JOBS["publish"]["environment"] == "siteops-release"
    assert JOBS["publish"]["permissions"] == {
        "contents": "write", "actions": "read", "attestations": "read",
    }
    for name in ("prepare", "review"):
        assert JOBS[name]["permissions"]["contents"] == "read"
    assert JOBS["publish"]["concurrency"]["cancel-in-progress"] is False


def test_shared_verification_runs_again_before_any_publication_write():
    names = [item["name"] for item in JOBS["publish"]["steps"]]
    assert names.index("Verify the approved candidate") < names.index("Create only the approved missing tag")
    assert names.index("Verify the approved bundle") < names.index("Create only the approved missing tag")
    assert names.index("Check the publication target") < names.index("Create only the approved missing tag")
    assert step("review", "Check the publication target")["run"] == step("publish", "Check the publication target")["run"]
    assert step("review", "Download the pinned declaration")["with"]["artifact-ids"] == "${{ needs.prepare.outputs.artifact-id }}"
    assert step("publish", "Download the qualified bundle")["with"]["artifact-ids"] == "${{ needs.candidate.outputs.bundle-artifact-id }}"
    assert not any("checkout@" in item.get("uses", "") for name in ("review", "publish") for item in JOBS[name]["steps"])
    assert not any("scripts/" in item.get("run", "") for item in JOBS["publish"]["steps"])


def test_declared_record_events_do_not_create_a_release_on_an_ordinary_push():
    events = WORKFLOW[True]
    assert events["push"]["branches"] == ["main"]
    assert set(events["push"]["paths"]) == {"releases/**/release.json", "releases/**/notes.md"}
    assert set(events["workflow_dispatch"]["inputs"]) == {"release-file", "expected-source-sha"}
    assert WORKFLOW["name"] == "Release (approval required)"
    assert JOBS["candidate"]["with"]["intent"] == "${{ inputs.release-file || '' }}"
    assert JOBS["candidate"]["with"]["expected-source-sha"] == (
        "${{ github.event_name != 'workflow_dispatch' && github.sha || inputs.expected-source-sha }}"
    )
    assert "pull_request" not in events


def test_operator_guide_matches_the_visible_workflow_controls():
    guide = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text())
    for name in ci[True]["workflow_dispatch"]["inputs"]:
        assert f"`{name}`" in guide
    for option in ci[True]["workflow_dispatch"]["inputs"]["run-mode"]["options"]:
        assert f"`{option}`" in guide
    assert WORKFLOW["name"] in guide
    assert "`release-intent`" not in guide and "`rehearsal`" not in guide
    assert not (ROOT / ".github" / "workflows" / "siteops-distribution.yaml").exists()
    assert ".github/release-examples/<name>/" in guide
    assert "releases/<name>/release.json" in guide


@pytest.mark.parametrize("expected", [SHA, "", "abc", "a" * 40])
def test_manual_source_request_requires_the_exact_commit(runner, expected):
    result, _, _ = runner("prepare", "Confirm the source request", extra={"EXPECTED_SHA": expected})
    assert result.returncode == (0 if expected == SHA else 1), result.stdout + result.stderr


@pytest.mark.parametrize("outcome", ["success", "failure", "missing", "other-sha", "other-workflow"])
def test_exact_source_ci_readiness_is_a_required_gate(candidate, runner, outcome):
    run = {
        "id": 10, "head_sha": SHA, "path": ".github/workflows/ci.yaml",
        "event": "push", "repository": {"full_name": REPO},
        "status": "completed", "conclusion": outcome,
        "html_url": f"https://github.com/{REPO}/actions/runs/10",
    }
    if outcome == "other-sha":
        run["head_sha"] = "a" * 40
    if outcome == "other-workflow":
        run["path"] = ".github/workflows/release.yaml"
    candidate["responses"][f"repos/{REPO}/actions/workflows/ci.yaml/runs"] = {
        "status": 200, "body": {"workflow_runs": [] if outcome == "missing" else [run]},
    }
    result, outputs, calls = runner(
        "review", "Require successful CI for the candidate", extra={"FAST_CI_CLOCK": "1"},
    )
    assert result.returncode == (0 if outcome == "success" else 1), result.stdout + result.stderr
    if outcome == "success":
        assert outputs["url"] == run["html_url"]
    assert all("--method" not in call or call[call.index("--method") + 1] == "GET" for call in calls)


def test_approval_preview_discloses_tag_authorization_notes_and_evidence(candidate, runner):
    runner("review", "Render the final release notes", extra={"ENGINE_VERSION": "1.0.0b1+build.42"})
    result, _, _ = runner(
        "review", "Show the release approval preview",
        extra={
            "ENGINE_VERSION": "1.0.0b1+build.42", "CI_URL": "https://github.com/example/publisher/actions/runs/10",
            "BUNDLE_SHA": "a" * 64, "TAG_EXISTS": "false",
            "MATRIX": json.dumps([
                {"python": version, "linux": "passed", "windows": "passed"}
                for version in ("3.10", "3.11", "3.12", "3.13", "3.14")
            ]),
            "ARTIFACT_URL": "https://github.com/example/publisher/actions/runs/42/artifacts/99",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = (candidate["root"].parent / "summary.md").read_text()
    for value in (SHA, "Create the missing tag", "Reviewed release notes", "Content", "Approval authorizes"):
        assert value.lower() in summary.lower()


def test_every_embedded_python_program_compiles_without_shell_indentation():
    count = 0
    for job in JOBS.values():
        for item in job.get("steps", []):
            for match in re.finditer(r"\bpython3?\s+-c\s+'([^']*)'", item.get("run", "")):
                compile(match[1], "<workflow>", "exec")
                count += 1
    assert count >= 10


def test_valid_preview_bundle_keeps_its_exact_source_and_artifact(runner):
    result, outputs, calls = runner("review", "Verify the pinned candidate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["engine-version"] == "1.0.0b1+build.42.1.gcccccccccccc"
    assert len(outputs["plan-sha"]) == len(outputs["bundle-sha"]) == 64
    verify = next(call for call in calls if call[:2] == ["attestation", "verify"])
    assert verify[verify.index("--source-digest") + 1] == SHA
    assert verify[verify.index("--signer-digest") + 1] == SHA
    assert "--deny-self-hosted-runners" in verify
    assert "--signer-repo" not in verify
    assert not any("--method" in call for call in calls)


@pytest.mark.parametrize("kind", ["siteops", "content", "combined"])
def test_real_git_declaration_and_cli_feed_the_candidate_controller(
    repository, tmp_path, candidate, runner, kind,
):
    if kind == "siteops":
        declaration = {"tag": "siteops/v1.2.3"}
        base = "1.2.3"
    elif kind == "combined":
        declaration = {"tag": "v1.0.0b8", "siteops": {"build": True}}
        base = "1.0.0b1"
    else:
        declaration = {"tag": "v2.0.0", "siteops": {"release": "siteops/v1.0.0"}}
        base = "9.0.0b1"
    _write_source_version(repository, f'__version__ = "{base}"\n')
    raw, notes = _write_record(repository, declaration, notes="Reviewed UTF-8 notes: \u03b1.\n".encode())
    sha = _commit(repository, "reviewed declaration")
    output = tmp_path / "prepared"
    result = subprocess.run(
        [sys.executable, "-B", str(CLI), "--repository", REPO, "--source-sha", sha,
         "--source-ref", "refs/heads/main", "--intent", "releases/candidate/release.json",
         "--output-dir", str(output)],
        cwd=repository, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    candidate["plan"], candidate["source_sha"] = plan, sha
    (candidate["root"] / "release-plan" / "release-notes.md").write_bytes(
        (output / "release-notes.md").read_bytes(),
    )
    for revision in (sha, "refs/heads/main"):
        for name, body in (("release.json", raw.decode()), ("notes.md", notes.decode())):
            candidate["responses"][f"repos/{REPO}/contents/releases/candidate/{name}?ref={revision}"] = {
                "status": 200, "raw": body,
            }
    if plan["siteops"]["bundle"]:
        version = base if kind == "siteops" else base + "+build.42.1.g" + sha[:12]
        with zipfile.ZipFile(candidate["root"] / "release-bundle" / ARCHIVE, "w") as archive:
            archive.writestr("bundle.json", json.dumps({
                "apiVersion": "siteops.install/v1", "kind": "SiteOpsBundle",
                "source": plan["source"], "build": {"number": 42, "attempt": 1},
                "package": {"name": "siteops", "version": version},
            }))
    else:
        tag = "siteops/v1.0.0"
        encoded = urllib.parse.quote(tag, safe="")
        candidate["responses"][f"repos/{REPO}/releases/tags/{encoded}"] = {
            "status": 200, "body": {"id": 71, "tag_name": tag, "draft": False,
                                  "assets": [{"name": ARCHIVE}, {"name": PROOF}]},
        }
        candidate["responses"][f"repos/{REPO}/git/ref/tags/{encoded}"] = {
            "status": 200, "body": {"object": {"sha": "d" * 40}},
        }
    result, outputs, _ = runner("review", "Verify the pinned candidate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["tag"] == declaration["tag"]
    assert outputs["bundle"] == str(kind != "content").lower()
    result, _, _ = runner(
        "review", "Render the final release notes",
        extra={"ENGINE_VERSION": outputs.get("engine-version", "")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "\u03b1" in (candidate["root"] / "publish-notes.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("fault", ["source", "notes", "latest", "version", "mode", "current-intent"])
def test_invalid_or_superseded_candidate_cannot_reach_tag_creation(candidate, runner, fault):
    plan = candidate["plan"]
    if fault == "source":
        plan["source"]["commit"] = "d" * 40
    elif fault == "notes":
        (candidate["root"] / "release-plan" / "release-notes.md").write_text("changed")
    elif fault == "latest":
        plan["release"]["latest"] = True
    elif fault == "version":
        plan["release"]["version"] = "1.0.0"
    elif fault == "mode":
        plan["siteops"]["versionMode"] = "source"
    else:
        candidate["responses"][f"repos/{REPO}/contents/releases/candidate/notes.md?ref=refs/heads/main"]["raw"] = "new intent"
    result, _, calls = runner("review", "Verify the pinned candidate")
    assert result.returncode != 0
    assert not any("--method" in call for call in calls)


@pytest.mark.parametrize("variable", ["FAIL_ATTESTATION", "APPROVED_PLAN_SHA", "APPROVED_BUNDLE_SHA", "APPROVED_NOTES_SHA"])
def test_authentication_and_approved_digest_drift_fail_closed(runner, variable):
    value = "1" if variable == "FAIL_ATTESTATION" else "a" * 64
    name = "Verify the approved bundle" if variable in {"FAIL_ATTESTATION", "APPROVED_BUNDLE_SHA"} else "Verify the approved candidate"
    result, _, calls = runner("publish", name, extra={variable: value})
    assert result.returncode != 0
    assert not any("--method" in call for call in calls)


def test_independent_content_uses_released_engine_without_bundle_verification(candidate, runner):
    plan = candidate["plan"]
    tag = "siteops/v1.0.0"
    raw = json.dumps({"tag": "v2.0.0", "siteops": {"release": tag}}).encode()
    plan["release"].update(tag="v2.0.0", version="2.0.0", prerelease=False, title="Content 2.0.0")
    plan["siteops"] = {"bundle": False, "versionMode": None, "baseVersion": None, "releaseTag": tag}
    plan["intent"]["sha256"] = digest(raw)
    for revision in (SHA, "refs/heads/main"):
        candidate["responses"][f"repos/{REPO}/contents/releases/candidate/release.json?ref={revision}"]["raw"] = raw.decode()
    encoded = urllib.parse.quote(tag, safe="")
    candidate["responses"][f"repos/{REPO}/releases/tags/{encoded}"] = {
        "status": 200, "body": {"id": 71, "tag_name": tag, "draft": False,
                              "assets": [{"name": ARCHIVE}, {"name": PROOF}]},
    }
    candidate["responses"][f"repos/{REPO}/git/ref/tags/{encoded}"] = {
        "status": 200, "body": {"object": {"sha": "d" * 40}},
    }
    result, outputs, calls = runner("review", "Verify the pinned candidate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["engine-id"] == "71"
    assert not any(call[0] == "attestation" for call in calls)
    candidate["responses"][f"repos/{REPO}/releases/tags/{encoded}"]["body"]["assets"] = []
    result, _, _ = runner("review", "Verify the pinned candidate")
    assert result.returncode != 0
    candidate["responses"][f"repos/{REPO}/releases/tags/{encoded}"]["body"]["assets"] = [
        {"name": ARCHIVE}, {"name": PROOF},
    ]
    result, _, _ = runner("publish", "Verify the approved candidate", extra={"APPROVED_ENGINE_ID": "71", "APPROVED_ENGINE_REF": "e" * 40})
    assert result.returncode != 0


@pytest.mark.parametrize("state", ["missing", "matching", "conflicting", "unavailable", "published"])
def test_tag_and_release_target_conditions(candidate, runner, state):
    tag = candidate["plan"]["release"]["tag"]
    candidate["responses"][f"repos/{REPO}/releases/tags/{tag}"] = {
        "status": 200 if state == "published" else 404, "body": {},
    }
    candidate["responses"][f"repos/{REPO}/git/ref/tags/{tag}"] = {
        "status": 404 if state == "missing" else 403 if state == "unavailable" else 200,
        "body": {"object": {"type": "commit", "sha": "d" * 40 if state == "conflicting" else SHA}},
    }
    result, outputs, calls = runner("review", "Check the publication target")
    assert result.returncode == (0 if state in {"missing", "matching"} else 1), result.stdout + result.stderr
    if result.returncode == 0:
        assert outputs["tag-exists"] == str(state == "matching").lower()
    assert not any("--method" in call for call in calls)


@pytest.mark.parametrize("configured", [False, True])
def test_publication_requires_real_environment_reviewers(candidate, runner, configured):
    assert JOBS["prepare"]["permissions"] == {"contents": "read", "actions": "read"}
    candidate["responses"][f"repos/{REPO}/environments/siteops-release"] = {
        "status": 200,
        "body": {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "Team"}]}] if configured else []},
    }
    result, _, _ = runner("prepare", "Require configured publication reviewers")
    assert result.returncode == (0 if configured else 1), result.stdout + result.stderr


def test_tag_creation_uses_only_the_approved_commit(candidate, runner):
    tag = candidate["plan"]["release"]["tag"]
    candidate["responses"][f"repos/{REPO}/git/refs"] = {
        "status": 201, "body": {"ref": "refs/tags/" + tag, "object": {"sha": SHA}},
    }
    result, _, calls = runner("publish", "Create only the approved missing tag")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [["api", "--method", "POST", f"repos/{REPO}/git/refs", "-f", "ref=refs/tags/" + tag, "-f", "sha=" + SHA]]
    assert "force" not in json.dumps(calls)


def test_notes_preview_and_publication_use_identical_rendering(candidate, runner):
    result, values, _ = runner(
        "review", "Render the final release notes",
        extra={"ENGINE_VERSION": "1.0.0b1+build.42.1.gcccccccccccc"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    notes = (candidate["root"] / "publish-notes.md").read_bytes()
    assert digest(notes) == values["sha256"]
    (candidate["root"] / "release-notes" / "publish-notes.md").write_bytes(notes)
    result, _, _ = runner("publish", "Verify the approved candidate", extra={"APPROVED_NOTES_SHA": values["sha256"]})
    assert result.returncode == 0
    (candidate["root"] / "release-notes" / "publish-notes.md").write_bytes(notes + b"changed")
    result, _, _ = runner(
        "publish", "Verify the approved candidate", extra={"APPROVED_NOTES_SHA": values["sha256"]},
    )
    assert result.returncode != 0


def test_release_rehearsal_cannot_reach_publishing_permissions():
    assert "publish" not in CANDIDATE_WORKFLOW["jobs"]
    for job in CANDIDATE_WORKFLOW["jobs"].values():
        assert job.get("permissions", {}).get("contents") != "write"
        assert "environment" not in job
    assert step("prepare", "Require configured publication reviewers")["if"] == "${{ !inputs.dry-run && steps.plan.outputs.active == 'true' }}"
    names = [item["name"] for item in JOBS["prepare"]["steps"]]
    assert names.index("Prepare the immutable declaration") < names.index("Require configured publication reviewers")
    assert names.index("Require configured publication reviewers") < names.index("Retain the declaration and notes")
    assert JOBS["distribution"]["with"]["report-summary"] is False
    text = yaml.safe_dump(CANDIDATE_WORKFLOW)
    assert "release create" not in text and "--method POST" not in text
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text())
    job = ci["jobs"]["release-preview"]
    assert job["needs"] == ["lint", "test", "validate"]
    assert job["with"]["dry-run"] is True
    assert job["permissions"]["contents"] == "read"


@pytest.mark.parametrize("is_dry_run", [True, False])
def test_example_branch_candidate_is_allowed_only_as_a_dry_run(candidate, runner, is_dry_run):
    plan = candidate["plan"]
    ref = "refs/heads/preview"
    plan["dryRun"] = is_dry_run
    plan["source"]["ref"] = ref
    plan["intent"]["path"] = ".github/release-examples/example/release.json"
    plan["intent"]["notesPath"] = ".github/release-examples/example/notes.md"
    for revision in (SHA, ref):
        for name in ("release.json", "notes.md"):
            candidate["responses"][f"repos/{REPO}/contents/.github/release-examples/example/{name}?ref={revision}"] = dict(
                candidate["responses"][f"repos/{REPO}/contents/releases/candidate/{name}?ref={SHA}"],
            )
    with zipfile.ZipFile(candidate["root"] / "release-bundle" / ARCHIVE, "w") as archive:
        archive.writestr("bundle.json", json.dumps({
            "apiVersion": "siteops.install/v1", "kind": "SiteOpsBundle", "source": plan["source"],
            "build": {"number": 42, "attempt": 1},
            "package": {"name": "siteops", "version": "1.0.0b1+build.42.1.gcccccccccccc"},
        }))
    extra = {"DRY_RUN": str(is_dry_run).lower(), "SOURCE_REF": ref}
    result, _, _ = runner("review", "Verify the pinned candidate", extra=extra)
    assert result.returncode == (0 if is_dry_run else 1), result.stdout + result.stderr
    result, _, calls = runner("publish", "Verify the approved candidate", extra=extra)
    assert result.returncode != 0
    assert not any("--method" in call for call in calls)


def test_dry_run_uses_completed_ci_jobs_without_waiting_for_its_own_run(candidate, runner):
    candidate["responses"][f"repos/{REPO}/actions/runs/42/attempts/1/jobs?per_page=100"] = {
        "status": 200, "body": {"jobs": [
            {"name": name, "conclusion": "success"} for name in ("Lint", "Unit Tests", "Validate Manifests")
        ]},
    }
    result, output, _ = runner("review", "Require successful CI for the candidate", extra={"DRY_RUN": "true"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert output["url"].endswith("/actions/runs/42")
    candidate["responses"][f"repos/{REPO}/actions/runs/42/attempts/1/jobs?per_page=100"]["body"]["jobs"].pop()
    result, _, _ = runner("review", "Require successful CI for the candidate", extra={"DRY_RUN": "true"})
    assert result.returncode != 0


def test_dry_run_summary_stops_at_preview_without_approval_instructions(candidate, runner):
    candidate["plan"]["dryRun"] = True
    runner("review", "Render the final release notes", extra={"ENGINE_VERSION": "1.0.0b1+build.42"})
    result, _, _ = runner(
        "review", "Show the release approval preview",
        extra={
            "DRY_RUN": "true", "ENGINE_VERSION": "1.0.0b1+build.42",
            "CI_URL": "https://github.com/example/publisher/actions/runs/42",
            "BUNDLE_SHA": "a" * 64, "TAG_EXISTS": "false",
            "MATRIX": json.dumps([
                {"python": version, "linux": "passed", "windows": "passed"}
                for version in ("3.10", "3.11", "3.12", "3.13", "3.14")
            ]),
            "ARTIFACT_URL": "https://github.com/example/publisher/actions/runs/42/artifacts/99",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = (candidate["root"].parent / "summary.md").read_text()
    assert summary.startswith("# Release preview (no publication)")
    assert "No tag, GitHub Release, or approval request was created." in summary
    assert "Approve and deploy" not in summary
    assert summary.count("| Python | Linux | Windows |") == 1
    assert "Download the attested installation bundle" in summary


def _render_install_notes(candidate, runner):
    result, values, _ = runner(
        "review", "Render the final release notes",
        extra={"ENGINE_VERSION": "1.0.0b1+build.42.1.gcccccccccccc"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    notes = (candidate["root"] / "publish-notes.md").read_text(encoding="utf-8")
    assert values["sha256"] == digest(notes.encode())
    return notes


def _installation_block(notes, language):
    match = re.search(r"```" + language + r"\n(.*?)\n```", notes, re.DOTALL)
    assert match is not None
    return match[1]


@pytest.mark.parametrize("tag", ["v1.0.0b8", "siteops/v1.1.0"])
def test_install_notes_bind_downloads_and_commands_to_the_selected_release(candidate, runner, tag):
    candidate["plan"]["release"]["tag"] = tag
    candidate["plan"]["release"]["stream"] = "siteops" if tag.startswith("siteops/") else "scalekit"
    notes = _render_install_notes(candidate, runner)
    assert notes.count("## Install Site Ops") == 1
    assert f"https://github.com/{REPO}/releases/download/{urllib.parse.quote(tag, safe='')}/{ARCHIVE}" in notes
    assert f"https://github.com/{REPO}/blob/{SHA}/docs/install-siteops.md" in notes
    for language in ("powershell", "bash"):
        block = _installation_block(notes, language)
        for flag, value in (
            ("--repo", REPO), ("--source-digest", SHA), ("--signer-digest", SHA),
            ("--source-ref", "refs/heads/main"),
            ("--cert-identity", f"https://github.com/{REPO}/.github/workflows/_siteops-distribution.yaml@refs/heads/main"),
        ):
            assert f'{flag} "{value}"' in block
        assert "--deny-self-hosted-runners" in block and "--signer-repo" not in block
        assert "GH_TOKEN" not in block and "--custom-trusted-root" not in block
        assert "curl" not in block
        assert block.index("gh attestation verify") < block.index("install.py")
    assert "pipx 1.17.2" in notes and "GitHub CLI 2.95.0" in notes
    assert "CPython 3.10-3.14" in notes and "glibc 2.17" in notes


def test_content_only_install_notes_link_to_the_engine_release_without_wrong_source_commands(candidate, runner):
    candidate["plan"]["siteops"] = {
        "bundle": False, "versionMode": None, "baseVersion": None, "releaseTag": "siteops/v1.0.0",
    }
    notes = _render_install_notes(candidate, runner)
    assert f"https://github.com/{REPO}/releases/tag/siteops%2Fv1.0.0" in notes
    assert "its own source commit" in notes
    assert "gh attestation verify" not in notes and "releases/download/" not in notes


@pytest.mark.parametrize(
    ("verify_exit", "extract_exit", "install_exit", "expected"),
    [(0, 0, 0, ["verify", "extract", "install"]),
     (9, 0, 0, ["verify"]), (0, 7, 0, ["verify", "extract"]), (0, 0, 8, ["verify", "extract", "install"])],
)
def test_generated_linux_install_commands_gate_extraction_and_installation(
    candidate, runner, tmp_path, verify_exit, extract_exit, install_exit, expected,
):
    block = _installation_block(_render_install_notes(candidate, runner), "bash")
    phases, arguments = tmp_path / "phases.log", tmp_path / "verify-args.log"
    stubs = """
gh() {
    printf '%s\\n' verify >> "$PHASES"
    printf '%s\\n' "$@" > "$VERIFY_ARGS"
    return "$VERIFY_EXIT"
}
mkdir() { return 0; }
python3() {
    if [[ "$*" == *zipfile* ]]; then
        printf '%s\\n' extract >> "$PHASES"
        return "$EXTRACT_EXIT"
    fi
    printf '%s\\n' install >> "$PHASES"
    return "$INSTALL_EXIT"
}
"""
    result = _run_script(stubs + block, tmp_path, {
        "PHASES": bash_path(phases), "VERIFY_ARGS": bash_path(arguments),
        "VERIFY_EXIT": str(verify_exit), "EXTRACT_EXIT": str(extract_exit), "INSTALL_EXIT": str(install_exit),
    })
    assert (result.returncode == 0) is (verify_exit == extract_exit == install_exit == 0)
    assert phases.read_text().splitlines() == expected
    args = arguments.read_text().splitlines()
    assert args[:3] == ["attestation", "verify", "./siteops-install.zip"]
    assert args[args.index("--source-digest") + 1] == SHA


@pytest.mark.skipif(not shutil.which("pwsh"), reason="PowerShell command coverage requires an installed PowerShell.")
@pytest.mark.parametrize(
    ("verify_exit", "extract_exit", "install_exit", "expected"),
    [(0, 0, 0, ["verify", "extract", "install"]),
     (9, 0, 0, ["verify"]), (0, 7, 0, ["verify", "extract"]), (0, 0, 8, ["verify", "extract", "install"])],
)
def test_generated_powershell_install_commands_gate_extraction_and_installation(
    candidate, runner, tmp_path, verify_exit, extract_exit, install_exit, expected,
):
    block = _installation_block(_render_install_notes(candidate, runner), "powershell")
    phases, arguments = tmp_path / "phases.log", tmp_path / "verify-args.json"
    stubs = """
function gh {
    Add-Content -LiteralPath $env:PHASES -Encoding utf8 -Value 'verify'
    ConvertTo-Json -InputObject @($args) -Compress | Set-Content -LiteralPath $env:VERIFY_ARGS -Encoding utf8
    $global:LASTEXITCODE = [int]$env:VERIFY_EXIT
}
function Expand-Archive {
    Add-Content -LiteralPath $env:PHASES -Encoding utf8 -Value 'extract'
    if ($env:EXTRACT_EXIT -ne '0') { throw 'Synthetic extraction failure' }
}
function python {
    Add-Content -LiteralPath $env:PHASES -Encoding utf8 -Value 'install'
    $global:LASTEXITCODE = [int]$env:INSTALL_EXIT
}
"""
    result = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", stubs + block],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PHASES": str(phases), "VERIFY_ARGS": str(arguments),
             "VERIFY_EXIT": str(verify_exit), "EXTRACT_EXIT": str(extract_exit), "INSTALL_EXIT": str(install_exit)},
    )
    assert (result.returncode == 0) is (verify_exit == extract_exit == install_exit == 0), result.stdout + result.stderr
    assert phases.read_text(encoding="utf-8-sig").splitlines() == expected
    args = json.loads(arguments.read_text(encoding="utf-8-sig"))
    assert args[:3] == ["attestation", "verify", ".\\siteops-install.zip"]
    assert args[args.index("--source-digest") + 1] == SHA


@pytest.mark.parametrize(
    ("authored", "expected"),
    [
        ("# Main title\n\n## Changes\n\nBody.\n", ["### Main title", "#### Changes"]),
        ("## Changes\n\nBody.\n", ["### Changes"]),
        ("Title\n=====\n\nChanges\n-------\n\nBody.\n", ["### Title", "#### Changes"]),
        ("# Title\n\n```bash\n# Leave this comment alone\n```\n\n~~~\n# Also literal\n~~~\n", ["### Title"]),
        ("# Title\n\n---\n---\n\n    # Indented example\n", ["### Title"]),
    ],
)
def test_summary_nests_markdown_headings_without_changing_published_notes(candidate, runner, authored, expected):
    (candidate["root"] / "release-plan" / "release-notes.md").write_text(authored, encoding="utf-8")
    published = _render_install_notes(candidate, runner)
    assert published.startswith(authored)
    before = (candidate["root"] / "publish-notes.md").read_bytes()
    result, _, _ = runner("review", "Show the release approval preview", extra={
        "DRY_RUN": "true", "ENGINE_VERSION": "1.0.0b1+build.42",
        "CI_URL": f"https://github.com/{REPO}/actions/runs/42",
        "BUNDLE_SHA": "a" * 64, "TAG_EXISTS": "false",
        "MATRIX": json.dumps([
            {"python": version, "linux": "passed", "windows": "passed"}
            for version in ("3.10", "3.11", "3.12", "3.13", "3.14")
        ]),
        "ARTIFACT_URL": f"https://github.com/{REPO}/actions/runs/42/artifacts/99",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    summary = (candidate["root"].parent / "summary.md").read_text()
    assert summary.startswith("# Release preview")
    assert "\n## Release notes\n" in summary
    for heading in expected:
        assert "\n" + heading + "\n" in summary
    for literal in ("# Leave this comment alone", "# Also literal", "    # Indented example", "---\n---"):
        if literal in authored:
            assert literal in summary
    assert (candidate["root"] / "publish-notes.md").read_bytes() == before


@pytest.mark.parametrize("bundle", [False, True])
def test_publication_uploads_only_declared_assets(runner, bundle):
    result, _, calls = runner(
        "publish", "Publish the approved release",
        extra={"TITLE": "Example release", "PRERELEASE": "true", "LATEST": "false", "BUNDLE": str(bundle).lower()},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    command = calls[-1]
    assert command[:3] == ["release", "create", "v1.0.0b8"]
    assert "--verify-tag" in command and "--latest=false" in command
    assert "--target" not in command and "--clobber" not in command
    assert any(value.endswith(ARCHIVE) for value in command) is bundle
    assert any(value.endswith(PROOF) for value in command) is bundle


@pytest.mark.parametrize("immutable", [False, True])
def test_published_asset_digests_and_immutable_release_are_checked(candidate, runner, immutable):
    tag = candidate["plan"]["release"]["tag"]
    assets = [
        {"name": name, "digest": "sha256:" + digest((candidate["root"] / "release-bundle" / name).read_bytes()), "state": "uploaded"}
        for name in (ARCHIVE, PROOF)
    ]
    endpoint = f"repos/{REPO}/releases/tags/{tag}"
    candidate["responses"][endpoint] = {
        "status": 200,
        "body": {"tag_name": tag, "draft": False, "prerelease": True, "immutable": immutable, "assets": assets},
    }
    result, _, calls = runner(
        "publish", "Confirm published assets and immutability",
        extra={"PRERELEASE": "true", "BUNDLE": "true"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(call[:2] == ["release", "verify"] for call in calls) is immutable
    assets[0]["digest"] = "sha256:" + "a" * 64
    result, _, _ = runner(
        "publish", "Confirm published assets and immutability",
        extra={"PRERELEASE": "true", "BUNDLE": "true"},
    )
    assert result.returncode != 0
