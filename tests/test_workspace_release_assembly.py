"""Verify collection order and routing using real packages and controlled proof results."""

import hashlib
import importlib.util
import json
import shutil
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from siteops.artifact_verification import ArtifactVerification
from siteops.artifacts import ArtifactError, hash_file
from siteops.workspace_source import (
    WORKSPACE_RELEASE_NAME,
    ArtifactIdentity,
    WorkspaceReleaseAssets,
)
from tests.release_helpers import SCRIPTS, _commit, _write_record, create_repository
from tests.workspace_release_helpers import _declaration, _load, _produce, _workspace

sys.path.insert(0, str(SCRIPTS))

import workspace_release_assembly as assembly  # noqa: E402
from siteops_release import bind_prepared_plan  # noqa: E402


class ControlledVerifier:
    def __init__(self):
        self.calls = []
        self.reject = None
        self.change = None

    def __call__(self, artifact, proof, identity):
        self.calls.append(artifact.name)
        if artifact.name == self.reject:
            raise ArtifactError("Controlled proof rejection.")
        assert hash_file(artifact, limit=identity.size) == (identity.size, identity.sha256)
        proof_sha = hashlib.sha256(proof.read_bytes()).hexdigest()
        now = datetime.now(timezone.utc)
        receipt = ArtifactVerification(
            identity.sha256, identity.size, "fixture-policy", 1, "a" * 64, "b" * 64,
            proof_sha, now, now + timedelta(hours=1), "fixture", "1", b"{}",
        )
        if self.change is not None:
            return self.change(artifact, proof, receipt)
        return receipt


@pytest.fixture(scope="module")
def staged_source(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("assembly-source")
    repository = create_repository(tmp_path / "repository")
    requests = [_workspace(repository, index=True), _workspace(repository, "second")]
    _write_record(repository, _declaration(requests))
    sha = _commit(repository, "workspace collection")
    staging = tmp_path / "staging"
    staging.mkdir()
    for slot, request in enumerate(requests, 1):
        root = staging / f"workspace-attested-42-3-{slot}"
        result = _produce(repository, sha, root, "--release-workspace", request["workspace"])
        assert result.returncode == 0, result.stdout + result.stderr
        for name in ("workspace-builds.json", request["package"]):
            (root / (name + ".attestation.jsonl")).write_bytes(b"controlled opaque proof")
    intent = _load(repository, sha)
    return {
        "intent": intent, "plan": bind_prepared_plan(intent), "staging": staging,
        "repository": repository,
    }


@pytest.fixture
def staged(staged_source, tmp_path):
    staging = tmp_path / "staging"
    shutil.copytree(staged_source["staging"], staging)
    return {
        **staged_source, "staging": staging,
        "output": tmp_path / "release", "verifier": ControlledVerifier(),
    }


def collect(staged):
    return assembly.assemble_workspace_assets(
        staged["intent"], staged["plan"], staged["staging"], staged["output"],
        build_number=42, build_attempt=3, verifier=staged["verifier"],
    )


def test_collection_generates_provider_neutral_routing_after_all_verification(staged, monkeypatch):
    original = assembly._write

    def after_verification(path, raw, created):
        assert len(staged["verifier"].calls) == 4
        return original(path, raw, created)

    monkeypatch.setattr(assembly, "_write", after_verification)
    inventory = collect(staged)
    assert staged["verifier"].calls == [
        "workspace-builds.json", "workspace.zip", "workspace-builds.json", "second.zip",
    ]
    descriptor = WorkspaceReleaseAssets.from_bytes(
        (staged["output"] / WORKSPACE_RELEASE_NAME).read_bytes(),
    )
    assert descriptor.revision == staged["intent"].source_sha
    assert {entry.workspace for entry in descriptor.workspaces} == {"workspace", "second"}
    assert descriptor.select("workspace").index_sha256 is not None
    assert descriptor.select("second").index_sha256 is None
    assert set(json.loads(descriptor.serialized())["source"]) == {"revision"}
    assert len(inventory.assets) == 5
    assert inventory.engine is None
    for identity in inventory.assets:
        assert hash_file(staged["output"] / identity.name, limit=identity.size) == (
            identity.size, identity.sha256,
        )
    assert {path.name for path in staged["output"].iterdir()} == {
        *(entry.name for entry in inventory.assets), "release-assets.json",
    }


@pytest.mark.parametrize("subject", ["workspace-builds.json", "workspace.zip", "second.zip"])
def test_failed_proof_produces_no_partial_descriptor_or_release(staged, subject):
    staged["verifier"].reject = subject
    with pytest.raises(ArtifactError, match="Controlled proof rejection"):
        collect(staged)
    assert not staged["output"].exists()


def test_build_record_is_not_parsed_before_its_proof_passes(staged, monkeypatch):
    staged["verifier"].reject = "workspace-builds.json"
    monkeypatch.setattr(assembly, "load_artifact_json", lambda *a, **k: pytest.fail("Record parsed before verification."))
    with pytest.raises(ArtifactError, match="Controlled proof rejection"):
        collect(staged)


def test_package_is_not_inspected_before_its_proof_passes(staged, monkeypatch):
    staged["verifier"].reject = "workspace.zip"
    monkeypatch.setattr(assembly, "inspect_produced_package", lambda *a, **k: pytest.fail("Package parsed before verification."))
    with pytest.raises(ArtifactError, match="Controlled proof rejection"):
        collect(staged)


@pytest.mark.parametrize("fault", ["plan", "source", "engine", "dry-run", "workspace", "kit", "name", "digest", "compatibility", "index"])
def test_collection_rejects_verified_but_inconsistent_build_claims(staged, fault):
    path = staged["staging"] / "workspace-attested-42-3-1" / "workspace-builds.json"
    record = json.loads(path.read_bytes())
    row = record["workspaces"][0]
    if fault == "plan":
        record["planSha256"] = "d" * 64
    elif fault == "source":
        record["source"]["commit"] = "d" * 40
    elif fault == "engine":
        record["engineVersion"] = "9.0.0"
    elif fault == "dry-run":
        record["dryRun"] = 0
    elif fault == "workspace":
        row["workspace"] = "second"
    elif fault == "kit":
        row["kit"]["version"] = "9"
    elif fault == "name":
        row["package"]["name"] = "other.zip"
    elif fault == "digest":
        row["package"]["sha256"] = "d" * 64
    elif fault == "compatibility":
        row["compatibility"]["siteops"] = ">=3,<4"
    else:
        row["index"]["sha256"] = "d" * 64
    path.write_text(json.dumps(record))
    with pytest.raises(ArtifactError):
        collect(staged)
    assert not staged["output"].exists()


@pytest.mark.parametrize("fault", ["extra-slot", "wrong-attempt", "extra-file", "missing-proof"])
def test_collection_requires_exact_staged_inventory(staged, fault):
    root = staged["staging"] / "workspace-attested-42-3-1"
    if fault == "extra-slot":
        (staged["staging"] / "unexpected").mkdir()
    elif fault == "wrong-attempt":
        root.rename(staged["staging"] / "workspace-attested-42-2-1")
    elif fault == "extra-file":
        (root / ".unexpected").write_text("unlisted")
    else:
        (root / "workspace.zip.attestation.jsonl").unlink()
    with pytest.raises(ArtifactError):
        collect(staged)
    assert not staged["output"].exists()


@pytest.mark.parametrize("fault", ["wrong-receipt", "changed-proof"])
def test_collection_rechecks_proof_identity_after_verifier(staged, fault):
    def change(artifact, proof, receipt):
        if fault == "wrong-receipt":
            return replace(receipt, sha256="d" * 64)
        proof.write_bytes(b"changed after verification")
        return receipt
    staged["verifier"].change = change
    message = "did not identify" if fault == "wrong-receipt" else "changed during verification"
    with pytest.raises(ArtifactError, match=message):
        collect(staged)
    assert not staged["output"].exists()


def test_collection_checks_verified_record_bytes_before_parsing(staged, monkeypatch):
    original = assembly.open_regular_file

    @contextmanager
    def change_before_read(path):
        if path.name == "workspace-builds.json":
            path.write_bytes(path.read_bytes() + b"\n")
        with original(path) as stream:
            yield stream

    monkeypatch.setattr(assembly, "open_regular_file", change_before_read)
    with pytest.raises(ArtifactError, match="changed before parsing"):
        collect(staged)
    assert not staged["output"].exists()


def test_collection_preserves_existing_output(staged):
    staged["output"].mkdir()
    marker = staged["output"] / "operator.txt"
    marker.write_text("preserved")
    with pytest.raises(FileExistsError):
        collect(staged)
    assert marker.read_text() == "preserved"
    assert not staged["verifier"].calls


@pytest.mark.parametrize("fault", [None, "repository", "ref", "roots", "policy-switch"])
def test_collection_cli_binds_independent_policy_to_selected_source(staged, tmp_path, monkeypatch, capsys, fault):
    path = SCRIPTS / "assemble-workspace-release.py"
    spec = importlib.util.spec_from_file_location("workspace_collection_cli_test", path)
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    intent = staged["intent"]
    roots = tmp_path / "roots.json"
    roots.write_bytes(b"independently supplied roots")
    root_sha = hashlib.sha256(roots.read_bytes()).hexdigest()
    policy = {
        "apiVersion": "siteops/v1alpha1", "kind": "ArtifactVerificationPolicy",
        "id": "fixture-policy", "version": 1,
        "validUntil": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "trustedRootSha256": root_sha if fault != "roots" else "d" * 64,
        "provider": {
            "kind": "github-attestation/v1",
            "repository": "other/repository" if fault == "repository" else intent.repository,
            "sourceRef": "refs/heads/other" if fault == "ref" else intent.source_ref,
            "signerWorkflow": ".github/workflows/_workspace-distribution.yaml",
            "builderWorkflow": ".github/workflows/release.yaml",
            "runnerEnvironment": "self-hosted",
        },
    }
    policy_path = tmp_path / "policy.json"
    raw = json.dumps(policy).encode()
    policy_path.write_bytes(raw)
    plan = tmp_path / "plan.json"
    from siteops_release import serialize_release_plan
    plan.write_bytes(serialize_release_plan(intent.to_dict()))

    def verify(artifact, proof, trusted_root, policy_file, *, expected_sha256, source_commit):
        assert trusted_root == roots and policy_file == policy_path
        assert source_commit == intent.source_sha
        receipt = staged["verifier"](
            artifact, proof, ArtifactIdentity(artifact.name, artifact.stat().st_size, expected_sha256),
        )
        return replace(
            receipt, root_sha256=root_sha,
            policy_sha256="d" * 64 if fault == "policy-switch" else hashlib.sha256(raw).hexdigest(),
        )

    monkeypatch.setattr(command, "verify_github_artifact", verify)
    monkeypatch.setattr(sys, "argv", [
        str(path), "--root", str(staged["repository"]), "--repository", intent.repository,
        "--source-sha", intent.source_sha, "--source-ref", intent.source_ref,
        "--release-file", intent.intent_path, "--prepared-plan", str(plan),
        "--expected-plan-sha", staged["plan"], "--staging", str(staged["staging"]),
        "--output", str(staged["output"]), "--build-number", "42", "--build-attempt", "3",
        "--trust-policy", str(policy_path), "--trusted-root", str(roots),
    ])
    result = command.main()
    if fault is None:
        assert result == 0
        assert json.loads(capsys.readouterr().out)["engineQualification"] == "not-established"
    else:
        assert result == 1 and not staged["output"].exists()
        if fault != "policy-switch":
            assert not staged["verifier"].calls
