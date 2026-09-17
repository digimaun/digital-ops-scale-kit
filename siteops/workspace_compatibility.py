# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Validate the engine version range shared by workspace authors and consumers."""

import unicodedata

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from siteops.artifacts import ArtifactError


def validate_engine_range(value: str) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 256
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ArtifactError("Workspace package text fields must be bounded and printable.")
    try:
        specifiers = SpecifierSet(value)
    except InvalidSpecifier:
        raise ArtifactError("The Site Ops compatibility range is invalid.") from None
    operators = {item.operator for item in specifiers}
    if "===" in operators or not (
        operators & {"==", "~="}
        or (operators & {">", ">="} and operators & {"<", "<="})
    ):
        raise ArtifactError("The Site Ops compatibility range must have lower and upper bounds.")
    return value


def require_engine_version(siteops_range: str, engine_version: str) -> None:
    """Require the target engine version to satisfy the bounded workspace declaration."""
    validate_engine_range(siteops_range)
    try:
        compatible = SpecifierSet(siteops_range).contains(Version(engine_version), prereleases=True)
    except (InvalidVersion, InvalidSpecifier):
        raise ArtifactError("The Site Ops compatibility declaration is invalid.") from None
    if not compatible:
        raise ArtifactError("This package requires a different Site Ops version.")
