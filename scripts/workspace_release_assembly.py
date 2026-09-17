# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Assemble public workspace routing from complete, verified subjects."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

from siteops_release import ReleaseIntent
from siteops_release_assets import FrozenReleaseAssets, ReleaseAsset
from workspace_release import BUILD_RECORD

from siteops.artifact_verification import ArtifactVerification
from siteops.artifacts import (
    ArtifactError,
    checked_path,
    hash_file,
    load_artifact_json,
    open_regular_file,
    require_node,
)
from siteops.content_index import INDEX_NAME
from siteops.workspace_package import (
    COMPILED_TEMPLATE_FEATURE,
    MAX_ARCHIVE_BYTES,
    inspect_produced_package,
    workspace_payload_path,
)
from siteops.workspace_source import (
    WORKSPACE_RELEASE_NAME,
    ArtifactIdentity,
    WorkspaceReleaseAssets,
    WorkspaceReleaseEntry,
)

PROOF_SUFFIX = ".attestation.jsonl"
MAX_PROOF_BYTES = 2 * 1024 * 1024
INVENTORY_NAME = "release-assets.json"
Verifier = Callable[[Path, Path, ArtifactIdentity], ArtifactVerification]
logger = logging.getLogger(__name__)


def _names(path: Path, maximum: int) -> set[str]:
    names = set()
    for child in path.iterdir():
        if len(names) == maximum:
            raise ArtifactError("The staged workspace inventory exceeds its expected size.")
        names.add(child.name)
    return names


def _identity(path: Path, maximum: int) -> ArtifactIdentity:
    size, digest = hash_file(path, limit=maximum)
    return ArtifactIdentity(path.name, size, digest)


def _verify(path: Path, proof: Path, maximum: int, verifier: Verifier) -> tuple[ArtifactIdentity, ArtifactIdentity]:
    artifact = _identity(path, maximum)
    evidence = _identity(proof, MAX_PROOF_BYTES)
    receipt = verifier(path, proof, artifact)
    if not isinstance(receipt, ArtifactVerification) or (
        receipt.sha256, receipt.size, receipt.proof_sha256
    ) != (artifact.sha256, artifact.size, evidence.sha256):
        raise ArtifactError("Workspace verification did not identify the selected bytes and proof.")
    if _identity(path, maximum) != artifact or _identity(proof, MAX_PROOF_BYTES) != evidence:
        raise ArtifactError("A workspace subject or proof changed during verification.")
    return artifact, evidence


def _copy(path: Path, output: Path, identity: ArtifactIdentity, created: list[Path]) -> None:
    target = output / identity.name
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created.append(target)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "wb") as outgoing, open_regular_file(path) as incoming:
        while chunk := incoming.read(min(1024 * 1024, identity.size - size + 1)):
            size += len(chunk)
            if size > identity.size:
                raise ArtifactError("A workspace input changed during collection.")
            outgoing.write(chunk)
            digest.update(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if (size, digest.hexdigest()) != (identity.size, identity.sha256):
        raise ArtifactError("A workspace input changed during collection.")


def _write(path: Path, raw: bytes, created: list[Path]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created.append(path)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def assemble_workspace_assets(
    intent: ReleaseIntent,
    plan_sha256: str,
    staging: Path,
    output: Path,
    *,
    build_number: int,
    build_attempt: int,
    verifier: Verifier,
) -> FrozenReleaseAssets:
    """Verify build record and package proofs, then create a new publication set.

    The caller supplies trusted verification code and a plan already bound to its
    independently expected digest. No receipt, package, or build record selects trust policy.
    """
    if not intent.workspaces:
        raise ArtifactError("The candidate has no declared workspace assets.")
    if (
        type(build_number) is not int or not 0 < build_number < 10**20
        or type(build_attempt) is not int or not 0 < build_attempt < 10**10
    ):
        raise ArtifactError("Workspace collection requires the exact run number and attempt.")
    require_node(staging, directory=True)
    require_node(output.parent, directory=True)
    staging, output = staging.resolve(), output.absolute()
    resolved_output = output.resolve()
    if resolved_output.is_relative_to(staging) or staging.is_relative_to(resolved_output):
        raise ArtifactError("Workspace input and output directories must be separate.")
    prefix = f"workspace-attested-{build_number}-{build_attempt}-"
    names = {prefix + str(slot) for slot in range(1, len(intent.workspaces) + 1)}
    if _names(staging, len(names)) != names:
        raise ArtifactError("The staged workspace set differs from the selected run and attempt.")
    engine_version = intent.engine_version(build_number, build_attempt)
    entries = []
    assets = []
    created: list[Path] = []
    output.mkdir(mode=0o700)
    complete = False
    try:
        for slot, request in enumerate(intent.workspaces, 1):
            root = checked_path(staging, prefix + str(slot), directory=True)
            if _names(root, 4) != {
                request.package_name, request.package_name + PROOF_SUFFIX,
                BUILD_RECORD, BUILD_RECORD + PROOF_SUFFIX,
            }:
                raise ArtifactError("A staged workspace does not contain its exact subject/proof pairs.")
            record_path = checked_path(root, BUILD_RECORD)
            verified_record, _ = _verify(
                record_path, checked_path(root, BUILD_RECORD + PROOF_SUFFIX),
                MAX_PROOF_BYTES, verifier,
            )
            with open_regular_file(record_path) as stream:
                raw = stream.read(MAX_PROOF_BYTES + 1)
            if (len(raw), hashlib.sha256(raw).hexdigest()) != (verified_record.size, verified_record.sha256):
                raise ArtifactError("The verified workspace record changed before parsing.")
            record = load_artifact_json(raw, limit=MAX_PROOF_BYTES, label="Verified workspace build record")
            if (
                type(record) is not dict or set(record) != {
                    "apiVersion", "kind", "source", "intent", "planSha256",
                    "dryRun", "engineVersion", "provenance", "workspaces",
                }
                or record["apiVersion"] != "siteops.release.workspaces/v1"
                or record["kind"] != "WorkspaceBuilds" or record["source"] != intent.to_dict()["source"]
                or record["intent"] != {"path": intent.intent_path, "sha256": intent.intent_sha256}
                or record["planSha256"] != plan_sha256 or record["dryRun"] is not intent.dry_run
                or record["engineVersion"] != engine_version or record["provenance"] != "not-established"
                or type(record["workspaces"]) is not list or len(record["workspaces"]) != 1
            ):
                raise ArtifactError("The verified build record describes a different candidate.")
            row = record["workspaces"][0]
            if (
                type(row) is not dict
                or not {"workspace", "kit", "package", "compatibility"} <= row.keys()
                or row.keys() - {"workspace", "kit", "package", "compatibility", "index"}
                or row["workspace"] != request.workspace
                or row["kit"] != {"id": request.kit_id, "version": intent.version}
            ):
                raise ArtifactError("The verified build record describes another workspace.")
            selected = ArtifactIdentity.from_document(row["package"])
            if selected.name != request.package_name:
                raise ArtifactError("The verified build record names another package.")
            package_path = checked_path(root, request.package_name)
            proof_path = checked_path(root, request.package_name + PROOF_SUFFIX)
            package, proof = _verify(package_path, proof_path, MAX_ARCHIVE_BYTES, verifier)
            if package != selected:
                raise ArtifactError("The verified package differs from its verified build record.")
            inspection = inspect_produced_package(
                package_path, package.sha256, engine_version=engine_version,
            )
            metadata = inspection.metadata
            if (
                metadata.source_revision != intent.source_sha or metadata.workspace_root != request.workspace
                or metadata.kit_id != request.kit_id or metadata.version != intent.version
                or metadata.siteops_range != request.siteops_range
                or set(metadata.required_features) != {
                    *(request.required_features or ("manifest/v1",)), COMPILED_TEMPLATE_FEATURE,
                }
                or metadata.document()["compatibility"] != row["compatibility"]
            ):
                raise ArtifactError("The package metadata differs from the reviewed workspace contract.")
            index_path = workspace_payload_path(request.workspace, INDEX_NAME)
            indexed = next((file for file in metadata.files if file.path == index_path), None)
            if (indexed is None and "index" in row) or (
                indexed is not None and row.get("index") != {"sha256": indexed.sha256}
            ):
                raise ArtifactError("The workspace index identity differs from its package.")
            entries.append(WorkspaceReleaseEntry(
                request.workspace, request.kit_id, intent.version, package, proof,
                indexed.sha256 if indexed is not None else None,
            ))
            for path, identity in ((package_path, package), (proof_path, proof)):
                _copy(path, output, identity, created)
                assets.append(ReleaseAsset(identity.name, identity.size, identity.sha256))
        descriptor = WorkspaceReleaseAssets(intent.source_sha, tuple(entries)).serialized()
        _write(output / WORKSPACE_RELEASE_NAME, descriptor, created)
        assets.append(ReleaseAsset(
            WORKSPACE_RELEASE_NAME, len(descriptor), hashlib.sha256(descriptor).hexdigest(),
        ))
        inventory = FrozenReleaseAssets(intent.repository, intent.source_sha, intent.source_ref, tuple(assets))
        _write(output / INVENTORY_NAME, inventory.serialized(), created)
        complete = True
        return inventory
    finally:
        if not complete:
            for path in reversed(created):
                try:
                    path.unlink()
                except OSError:
                    logger.warning("An incomplete workspace collection file could not be removed.")
            try:
                output.rmdir()
            except OSError:
                logger.warning("The incomplete workspace collection directory was retained.")
