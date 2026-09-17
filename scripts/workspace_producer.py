# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Build workspace packages from an exact Git revision for local or release use."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from source_snapshot import (
    export_tracked_source,
    require_complete_export,
    require_workspace_configuration_boundary,
)

from siteops import __version__
from siteops.artifacts import ArtifactError, checked_path, open_regular_file
from siteops.browse import BrowseError
from siteops.content_index import (
    BINDINGS_NAME,
    INDEX_NAME,
    MAX_INDEX_BYTES,
    build_content_index,
    load_source_bindings,
    write_content_index,
)
from siteops.package_builder import (
    build_package,
    create_producer_compilation_session,
    workspace_bicep_sources,
)
from siteops.workspace_package import PackageInspection


def _index_identity(workspace: Path) -> str | None:
    if not any((workspace / name).exists() for name in (INDEX_NAME, BINDINGS_NAME)):
        return None
    documents = []
    for name in (INDEX_NAME, BINDINGS_NAME):
        with open_regular_file(checked_path(workspace, name)) as stream:
            raw = stream.read(MAX_INDEX_BYTES + 1)
        if len(raw) > MAX_INDEX_BYTES:
            raise BrowseError("index.output", "A committed index exceeds its byte limit.")
        documents.append(raw)
    index, raw_bindings = documents
    bindings = load_source_bindings(raw_bindings, index)
    algorithms = {
        algorithm for item in bindings.inputs for algorithm, _ in (item.digests or ())
    } - {"sha256"}
    additional = None
    if algorithms:
        from siteops.github_catalog import github_input_digests

        if algorithms != set(github_input_digests(b"")):
            raise BrowseError("index.binding_settings", "The committed index uses unsupported source binding settings.")
        additional = github_input_digests
    expected = build_content_index(workspace, approve_public=True, additional_digests=additional)
    write_content_index(workspace, expected, check=True)
    return hashlib.sha256(index).hexdigest()


def build_committed_workspace(
    root: Path,
    source_sha: str,
    output: Path,
    *,
    workspace: str,
    kit_id: str,
    version: str,
    siteops_range: str,
    includes: tuple[str, ...],
    licenses: tuple[str, ...],
    required_features: tuple[str, ...] = ("manifest/v1",),
    engine_version: str = __version__,
    bicep_path: Path | None = None,
    check_index: bool = False,
) -> tuple[PackageInspection, str | None]:
    """Build from committed blobs after the caller validates the checkout."""
    companions = tuple(dict.fromkeys((*includes, *licenses)))
    paths = (workspace, *companions)
    with tempfile.TemporaryDirectory(prefix="siteops-package-source-") as temporary:
        control_root = Path(temporary)
        snapshot = control_root / "source"
        export_tracked_source(root, snapshot, source_sha, paths=paths)
        require_complete_export(root, source_sha, paths, snapshot)
        bicep_sources = workspace_bicep_sources(snapshot, workspace)
        if bicep_sources:
            require_workspace_configuration_boundary(root, source_sha, workspace)
        for license_path in licenses:
            try:
                checked_path(snapshot, license_path)
            except ArtifactError:
                raise ArtifactError("A declared package license file is missing or invalid.") from None
        content_root = snapshot if workspace == "." else checked_path(snapshot, workspace, directory=True)
        index = _index_identity(content_root) if check_index else None
        result = build_package(
            snapshot, output, workspace=workspace, kit_id=kit_id, version=version,
            source_revision=source_sha, siteops_range=siteops_range,
            companions=companions, required_features=required_features,
            engine_version=engine_version,
            compilation_session_factory=lambda: create_producer_compilation_session(
                snapshot, control_root, bicep_path=bicep_path,
            ),
        )
    return result, index
