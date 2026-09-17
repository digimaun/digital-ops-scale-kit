# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Read reviewed workspace build inputs without compiling content or loading Site values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from siteops_release_assets import ARCHIVE_NAME, PROOF_SUFFIX, validate_asset_name

from siteops.artifacts import relative_artifact_path
from siteops.workspace_compatibility import require_engine_version, validate_engine_range

MAX_WORKSPACE_BUILDS = 64
WORKSPACE_DESCRIPTOR = "siteops-workspaces.json"
BUILD_RECORD = "workspace-builds.json"


class WorkspaceReleaseError(ValueError):
    """The declaration does not define one complete, unambiguous workspace asset set."""


def _record(
    value: Any, required: set[str], optional: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if type(value) is not dict or not required <= value.keys() or value.keys() - required - optional:
        raise WorkspaceReleaseError("Workspace build inputs contain unsupported or missing fields.")
    return value


def _paths(value: Any, *, required: bool) -> tuple[str, ...]:
    if type(value) is not list or not (int(required) <= len(value) <= 64):
        raise WorkspaceReleaseError("Workspace companion paths require a bounded list.")
    result = tuple(relative_artifact_path(path) for path in value)
    if len({path.casefold() for path in result}) != len(result):
        raise WorkspaceReleaseError("Workspace companion paths must be unique without case aliases.")
    return result


@dataclass(frozen=True)
class WorkspaceBuild:
    workspace: str
    kit_id: str
    package_name: str
    siteops_range: str
    licenses: tuple[str, ...]
    includes: tuple[str, ...] | None = None
    required_features: tuple[str, ...] | None = None

    @classmethod
    def from_document(cls, value: Any) -> WorkspaceBuild:
        row = _record(
            value, {"workspace", "id", "package", "compatibility", "licenses"}, {"include"},
        )
        workspace = row["workspace"]
        if workspace != ".":
            relative_artifact_path(workspace)
        kit_id = row["id"]
        if (
            type(kit_id) is not str or not 1 <= len(kit_id) <= 128
            or kit_id != kit_id.strip() or not kit_id.isprintable()
        ):
            raise WorkspaceReleaseError("The workspace kit ID must be bounded printable text.")
        name = validate_asset_name(row["package"])
        if not name.endswith(".zip") or name.casefold() == ARCHIVE_NAME:
            raise WorkspaceReleaseError("Each workspace requires its own portable .zip filename.")
        validate_asset_name(name + PROOF_SUFFIX)
        compatibility = _record(row["compatibility"], {"siteops"}, {"requiredFeatures"})
        engine_range = validate_engine_range(compatibility["siteops"])
        features = compatibility.get("requiredFeatures")
        if features is not None:
            if (
                type(features) is not list or not 1 <= len(features) <= 64
                or any(
                    type(feature) is not str or len(feature) > 128
                    or re.fullmatch(r"[a-z][a-z0-9-]*/v[1-9][0-9]*", feature) is None
                    for feature in features
                )
                or len(set(features)) != len(features)
            ):
                raise WorkspaceReleaseError("Required workspace features must be a bounded unique list.")
            features = tuple(features)
        elif "requiredFeatures" in compatibility:
            raise WorkspaceReleaseError("Required workspace features must be a bounded unique list.")
        return cls(
            workspace, kit_id, name, engine_range, _paths(row["licenses"], required=True),
            _paths(row["include"], required=False) if "include" in row else None, features,
        )

    def document(self) -> dict[str, Any]:
        compatibility: dict[str, Any] = {"siteops": self.siteops_range}
        if self.required_features is not None:
            compatibility["requiredFeatures"] = list(self.required_features)
        row = {
            "workspace": self.workspace, "id": self.kit_id, "package": self.package_name,
            "compatibility": compatibility, "licenses": list(self.licenses),
        }
        if self.includes is not None:
            row["include"] = list(self.includes)
        return row

    def require_engine(self, version: str) -> None:
        require_engine_version(self.siteops_range, version)


def load_workspace_builds(value: Any, *, engine_version: str) -> tuple[WorkspaceBuild, ...]:
    if type(value) is not list or not 1 <= len(value) <= MAX_WORKSPACE_BUILDS:
        raise WorkspaceReleaseError("Declare between one and 64 workspace builds.")
    requests = tuple(WorkspaceBuild.from_document(row) for row in value)
    roots: set[str] = set()
    names: set[str] = {ARCHIVE_NAME, ARCHIVE_NAME + PROOF_SUFFIX, WORKSPACE_DESCRIPTOR, BUILD_RECORD}
    for request in requests:
        root = request.workspace.casefold()
        if root in roots:
            raise WorkspaceReleaseError("A release cannot build the same workspace more than once.")
        roots.add(root)
        for name in (request.package_name, request.package_name + PROOF_SUFFIX):
            if name.casefold() in names:
                raise WorkspaceReleaseError("Workspace release asset names must be unique.")
            names.add(name.casefold())
        request.require_engine(engine_version)
    return requests
