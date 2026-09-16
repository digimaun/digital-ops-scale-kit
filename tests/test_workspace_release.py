"""Reviewed workspace production without provider access or deployment commands."""

import copy
import hashlib
import json
import shutil
import subprocess
import sys

import pytest

from siteops.artifacts import ArtifactError
from siteops.content_index import build_content_index, write_content_index
from siteops.github_catalog import github_input_digests
from siteops.workspace_package import extract_package, inspect_package, inspect_produced_package
from tests.release_helpers import (
    REPOSITORY,
    ROOT,
    SCRIPTS,
    SOURCE_REF,
    _commit,
    _run_cli,
    _write_record,
    _write_source_version,
)
from tests.release_helpers import repository as repository

sys.path.insert(0, str(SCRIPTS))

from siteops_release import ReleaseIntentError, load_release_intent  # noqa: E402

PRODUCER = SCRIPTS / "build-workspace-release.py"


def _workspace(repository, name="workspace", *, index=False, github=False):
    root = repository / name
    shutil.copytree(ROOT / "tests" / "fixtures" / "browse-workspace", root)
    (repository / "LICENSE").write_text("Owned fixture license\n")
    if index:
        write_content_index(root, build_content_index(
            root, approve_public=True, additional_digests=github_input_digests if github else None,
        ))
    return {
        "workspace": name, "id": "fixture.storage", "package": name + ".zip",
        "compatibility": {"siteops": ">=1.2,<2"}, "licenses": ["LICENSE"],
    }


def _declaration(requests, *, combined=False):
    return {
        "tag": "v2.0.0b1" if combined else "v2.0.0",
        "siteops": {"build": True} if combined else {"release": "siteops/v1.2.3"},
        "workspaces": requests,
    }


def _load(repository, sha):
    return load_release_intent(
        repository, sha, "releases/candidate/release.json", REPOSITORY, SOURCE_REF,
    )


def _produce(repository, sha, output, *extra):
    return subprocess.run(
        [sys.executable, "-B", str(PRODUCER), "--root", str(repository),
         "--repository", REPOSITORY, "--expected-source-sha", sha,
         "--source-ref", SOURCE_REF, "--release-file", "releases/candidate/release.json",
         "--output-dir", str(output), *extra],
        cwd=output.parent, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        timeout=120, check=False,
    )


def test_workspace_declaration_is_source_bound_and_does_not_import_referenced_engine(repository):
    request = _workspace(repository)
    _write_source_version(repository, 'raise RuntimeError("never import candidate source")\n')
    raw, _ = _write_record(repository, _declaration([request]))
    sha = _commit(repository, "workspace intent")
    intent = _load(repository, sha)
    assert intent.to_dict()["workspaces"] == [request]
    assert intent.intent_sha256 == hashlib.sha256(raw).hexdigest()
    assert intent.engine_version() == "1.2.3"
    assert intent.version == "2.0.0" and intent.components == "content"
    request["id"] = "different"
    _write_record(repository, _declaration([request]))
    _commit(repository, "advance source")
    assert _load(repository, sha).workspaces[0].kit_id == "fixture.storage"


@pytest.mark.parametrize("combined", [False, True])
def test_real_release_producer_emits_exact_packages_without_proofs(repository, tmp_path, combined):
    requests = [_workspace(repository), _workspace(repository, "second")]
    _write_source_version(repository, '__version__ = "1.2.3"\n')
    _write_record(repository, _declaration(requests, combined=combined))
    sha = _commit(repository, "produce workspace candidate")
    output = tmp_path / "assets"
    extra = ("--build-number", "42", "--build-attempt", "3") if combined else ()
    result = _produce(repository, sha, output, *extra)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    raw = (output / "workspace-builds.json").read_bytes()
    assert summary["sha256"] == hashlib.sha256(raw).hexdigest()
    record = json.loads(raw)
    assert record["source"] == {"repository": REPOSITORY, "commit": sha, "ref": SOURCE_REF}
    assert record["engineVersion"] == (
        "1.2.3+build.42.3.g" + sha[:12] if combined else "1.2.3"
    )
    assert record["provenance"] == summary["provenance"] == "not-established"
    assert {path.name for path in output.iterdir()} == {
        "workspace.zip", "second.zip", "workspace-builds.json",
    }
    for row in record["workspaces"]:
        archive = output / row["package"]["name"]
        inspected = inspect_produced_package(
            archive, row["package"]["sha256"], engine_version=record["engineVersion"],
        )
        assert archive.stat().st_size == row["package"]["size"]
        assert inspected.metadata.source_revision == sha
        assert inspected.metadata.kit_id == row["kit"]["id"] == "fixture.storage"
        assert inspected.metadata.version == row["kit"]["version"] == ("2.0.0b1" if combined else "2.0.0")
        assert inspected.metadata.workspace_root == row["workspace"]
        assert "compiled-templates/v1" in row["compatibility"]["requiredFeatures"]
        assert inspected.metadata.templates[0].producer_mode == "native-arm-json"
        with pytest.raises(ArtifactError, match="different Site Ops version"):
            inspect_package(archive, row["package"]["sha256"])
        destination = tmp_path / ("consumer-" + row["workspace"])
        with pytest.raises(ArtifactError, match="different Site Ops version"):
            extract_package(archive, row["package"]["sha256"], destination)
        assert not destination.exists()


@pytest.mark.parametrize("github", [False, True])
def test_release_index_check_preserves_existing_source_binding_settings(repository, tmp_path, github):
    request = _workspace(repository, index=True, github=github)
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "indexed candidate")
    output = tmp_path / "assets"
    result = _produce(repository, sha, output)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((output / "workspace-builds.json").read_text())
    assert record["workspaces"][0]["index"]["sha256"] == hashlib.sha256(
        (repository / "workspace" / "siteops-index.json").read_bytes(),
    ).hexdigest()


@pytest.mark.parametrize("fault", ["stale", "missing-bindings", "missing-index", "unknown-algorithm"])
def test_release_rejects_incomplete_or_stale_committed_indexes(repository, tmp_path, fault):
    request = _workspace(repository, index=True)
    root = repository / "workspace"
    if fault == "stale":
        manifest = root / "manifests" / "storage" / "manifest.yaml"
        manifest.write_bytes(manifest.read_bytes() + b"\n# new reviewed input\n")
    elif fault == "missing-bindings":
        (root / "siteops-index.inputs.json").unlink()
    elif fault == "missing-index":
        (root / "siteops-index.json").unlink()
    else:
        path = root / "siteops-index.inputs.json"
        document = json.loads(path.read_text())
        next(row for row in document["inputs"] if row["digests"])["digests"]["other-sha256"] = "a" * 64
        path.write_text(json.dumps(document))
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "invalid index")
    output = tmp_path / "assets"
    result = _produce(repository, sha, output)
    assert result.returncode != 0
    assert not result.stdout and not output.exists()


@pytest.mark.parametrize("fault", ["dirty", "wrong-sha", "export-ignore", "second-package"])
def test_release_producer_failure_preserves_output_absence(repository, tmp_path, fault):
    request = _workspace(repository)
    requests = [request]
    if fault == "export-ignore":
        (repository / ".gitattributes").write_text("workspace/manifests export-ignore\n")
    elif fault == "second-package":
        requests.append(_workspace(repository, "second"))
        template = repository / "second" / "templates" / "storage.template.json"
        template.write_text("not an ARM template")
    _write_record(repository, _declaration(requests))
    sha = _commit(repository, "candidate")
    if fault == "dirty":
        (repository / "LICENSE").write_text("uncommitted")
    elif fault == "wrong-sha":
        sha = "a" * 40
    output = tmp_path / "assets"
    result = _produce(repository, sha, output)
    assert result.returncode != 0 and not result.stdout
    assert not output.exists()


def test_release_producer_preserves_existing_directory_and_files(repository, tmp_path):
    request = _workspace(repository)
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "candidate")
    output = tmp_path / "assets"
    output.mkdir()
    marker = output / "operator.txt"
    marker.write_text("preserve")
    result = _produce(repository, sha, output)
    assert result.returncode != 0
    assert marker.read_text() == "preserve"


@pytest.mark.parametrize("fault", [
    "engine-only", "empty", "too-many", "duplicate-root", "duplicate-name", "reserved-name",
    "path", "license", "directory-license", "unknown-field", "unbounded-range",
    "wrong-engine", "missing-feature", "duplicate-feature",
])
def test_invalid_workspace_declarations_fail_during_source_preparation(repository, fault):
    request = _workspace(repository)
    declaration = _declaration([request])
    if fault == "engine-only":
        declaration = {"tag": "siteops/v1.2.3", "workspaces": [request]}
        _write_source_version(repository, '__version__ = "1.2.3"\n')
    elif fault == "empty":
        declaration["workspaces"] = []
    elif fault == "too-many":
        declaration["workspaces"] = [request] * 65
    elif fault in {"duplicate-root", "duplicate-name"}:
        other = copy.deepcopy(request)
        other["package" if fault == "duplicate-root" else "workspace"] = "other.zip"
        declaration["workspaces"].append(other)
    elif fault == "reserved-name":
        request["package"] = "siteops-install.zip"
    elif fault == "path":
        request["workspace"] = "../workspace"
    elif fault == "license":
        request["licenses"] = ["absent"]
    elif fault == "directory-license":
        request["licenses"] = ["workspace"]
    elif fault == "unknown-field":
        request["command"] = "unreviewed hook"
    elif fault == "unbounded-range":
        request["compatibility"]["siteops"] = ">=1"
    elif fault == "wrong-engine":
        request["compatibility"]["siteops"] = ">=3,<4"
    elif fault == "missing-feature":
        request["compatibility"]["requiredFeatures"] = None
    else:
        request["compatibility"]["requiredFeatures"] = ["manifest/v1", "manifest/v1"]
    _write_record(repository, declaration)
    sha = _commit(repository, "invalid intent")
    with pytest.raises(ReleaseIntentError):
        _load(repository, sha)


def test_prepare_cli_retains_the_exact_workspace_request(repository, tmp_path):
    request = _workspace(repository)
    request["include"] = []
    request["compatibility"]["requiredFeatures"] = ["manifest/v1", "composition/v1"]
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "candidate")
    output = tmp_path / "plan"
    result = _run_cli(
        repository, "--repository", REPOSITORY, "--source-sha", sha, "--source-ref", SOURCE_REF,
        "--intent", "releases/candidate/release.json", "--output-dir", str(output),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((output / "plan.json").read_text())["workspaces"] == [request]
