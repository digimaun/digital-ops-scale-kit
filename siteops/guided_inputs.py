# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Resolve one declarative set of typed answers into an ordinary Site."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from siteops import yamlio
from siteops.artifacts import open_regular_file
from siteops.cache_layout import write_new
from siteops.models import Site, _validate_resource
from siteops.workspace_package import MaterializedPackageBinding

_VERSION = "siteops.inputs/v1"
_MAX_YAML_BYTES = 128 * 1024
_MAX_INPUTS = 64
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_ROOT_FIELDS = {"name", "subscription", "resourceGroup", "location"}
_MAPPING_FIELDS = {"labels", "parameters", "properties"}
_FIELD_KEYS = {
    "name", "type", "description", "sitePath", "required", "default", "sensitive", "when",
}
_MISSING = object()


class GuidedInputError(ValueError):
    """A guided-input validation failure with no supplied values or local paths."""


def contract_path(manifest_path: Path) -> Path:
    """Choose one manifest-specific sidecar without inspecting the filesystem."""
    if manifest_path.suffix not in {".yaml", ".yml"}:
        raise GuidedInputError("The manifest must have a .yaml or .yml filename.")
    if manifest_path.stem == "manifest":
        return manifest_path.with_name("inputs.yaml")
    return manifest_path.with_name(f"{manifest_path.stem}.inputs.yaml")


def _read_yaml(path: Path, *, label: str) -> Any:
    if not path.exists() and not path.is_symlink():
        raise GuidedInputError(f"{label} was not found.") from None
    with open_regular_file(path) as stream:
        raw = stream.read(_MAX_YAML_BYTES + 1)
    if len(raw) > _MAX_YAML_BYTES:
        raise GuidedInputError(f"{label} exceeds the 128 KiB YAML limit.")
    try:
        return yamlio.load_bounded(raw.decode("utf-8"))
    except yamlio.DuplicateKeyError:
        raise GuidedInputError(f"{label} contains a duplicate YAML key.") from None
    except (UnicodeError, yaml.YAMLError, RecursionError):
        raise GuidedInputError(f"{label} must be valid UTF-8 YAML within structural limits.") from None


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GuidedInputError(f"{label} must be a mapping.")
    if any(not isinstance(key, str) for key in value):
        raise GuidedInputError(f"{label} must have string keys.")
    return value


def _shape(value: dict[str, Any], *, allowed: set[str], required: set[str], label: str) -> None:
    if value.keys() - allowed:
        raise GuidedInputError(f"{label} contains an unknown key.")
    if required - value.keys():
        raise GuidedInputError(f"{label} is missing a required key.")


def _site_defaults(value: Any) -> dict[str, Any]:
    defaults = _mapping(value, "siteDefaults")
    _shape(defaults, allowed=_MAPPING_FIELDS, required=set(), label="siteDefaults")
    for name, content in defaults.items():
        _mapping(content, f"siteDefaults.{name}")
    pending = [defaults]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            _mapping(item, "siteDefaults nested mapping")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise GuidedInputError("siteDefaults contains an unsupported YAML value.")
    return defaults


def _typed_value(value: Any, kind: str, label: str, *, required: bool) -> None:
    if kind == "string":
        if not isinstance(value, str):
            raise GuidedInputError(f"{label} must be a string.")
        if required and not value.strip():
            raise GuidedInputError(f"{label} is required and must be a nonempty string.")
    elif type(value) is not bool:
        raise GuidedInputError(f"{label} must be a boolean (true or false).")


def _site_path(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise GuidedInputError("Input sitePath must be a dotted Site field.")
    try:
        size = len(value.encode("ascii"))
    except UnicodeEncodeError:
        raise GuidedInputError("Input sitePath must use ASCII identifiers.") from None
    if size > 256:
        raise GuidedInputError("Input sitePath exceeds the 256-byte limit.")
    parts = tuple(value.split("."))
    if (
        parts[0] in _ROOT_FIELDS and len(parts) == 1
        or parts[0] == "labels" and len(parts) == 2
        or parts[0] in {"parameters", "properties"} and len(parts) >= 2
    ) and len(parts) <= 8 and all(
        len(part) <= 64 and _IDENTIFIER.fullmatch(part) for part in parts
    ):
        return parts
    raise GuidedInputError(
        "Input sitePath must name a supported Site field with at most "
        "8 segments of 64 characters, without indexes or wildcards."
    )


@dataclass(frozen=True)
class InputCondition:
    input: str
    equals: str | bool


@dataclass(frozen=True)
class InputField:
    name: str
    type: str
    description: str
    site_path: tuple[str, ...]
    required: bool
    sensitive: bool
    default: str | bool | object = _MISSING
    when: InputCondition | None = None

    @property
    def has_default(self) -> bool:
        return self.default is not _MISSING


def _parse_fields(value: Any) -> tuple[InputField, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_INPUTS:
        raise GuidedInputError("Contract inputs must be a nonempty list of at most 64 fields.")
    fields: list[InputField] = []
    by_name: dict[str, InputField] = {}
    for index, item in enumerate(value, start=1):
        label = f"Input declaration {index}"
        row = _mapping(item, label)
        _shape(
            row,
            allowed=_FIELD_KEYS,
            required={"name", "type", "description", "sitePath"},
            label=label,
        )
        name = row["name"]
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
            raise GuidedInputError(f"{label} name must be a simple ASCII identifier.")
        if name in by_name:
            raise GuidedInputError(f"Contract inputs contain a duplicate name: {name}.")
        kind = row["type"]
        if kind not in ("string", "boolean"):
            raise GuidedInputError(f"{label} type must be string or boolean.")
        description = row["description"]
        if not isinstance(description, str) or not description.strip():
            raise GuidedInputError(f"{label} description must be nonempty text.")
        path = _site_path(row["sitePath"])
        if any(
            path[: len(field.site_path)] == field.site_path
            or field.site_path[: len(path)] == path
            for field in fields
        ):
            raise GuidedInputError(f"{label} sitePath overlaps another input writer path.")
        default = row.get("default", _MISSING)
        required = row.get("required", default is _MISSING)
        sensitive = row.get("sensitive", False)
        if type(required) is not bool:
            raise GuidedInputError(f"{label} required must be a boolean.")
        if type(sensitive) is not bool:
            raise GuidedInputError(f"{label} sensitive must be a boolean.")
        if sensitive:
            raise GuidedInputError(
                f"{label} sensitive inputs are unsupported until protected Site output is available."
            )
        if default is not _MISSING:
            _typed_value(default, kind, f"{label} default", required=required)
        when = None
        if "when" in row:
            condition = _mapping(row["when"], f"{label} when")
            _shape(
                condition,
                allowed={"input", "equals"},
                required={"input", "equals"},
                label=f"{label} when",
            )
            controller = condition["input"]
            if not isinstance(controller, str) or controller not in by_name:
                raise GuidedInputError(f"{label} when must reference an earlier declared input.")
            if by_name[controller].when is not None:
                raise GuidedInputError(f"{label} when cannot reference a conditional controller.")
            _typed_value(
                condition["equals"],
                by_name[controller].type,
                f"{label} when equals",
                required=False,
            )
            when = InputCondition(controller, condition["equals"])
        field = InputField(name, kind, description, path, required, sensitive, default, when)
        fields.append(field)
        by_name[name] = field
    return tuple(fields)


def _validate_default_writers(
    defaults: dict[str, Any], fields: tuple[InputField, ...],
) -> None:
    for field in fields:
        current: Any = defaults
        for part in field.site_path:
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            if isinstance(current, dict):
                raise GuidedInputError(
                    f"Input '{field.name}' sitePath would replace a siteDefaults mapping."
                )


@dataclass(frozen=True)
class InputContract:
    """A closed, versioned contract that resolves file and inline answers into one Site."""

    fields: tuple[InputField, ...]
    _site_defaults: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_site_defaults", copy.deepcopy(self._site_defaults))

    @property
    def inputs(self) -> tuple[InputField, ...]:
        return self.fields

    def describe(self) -> dict[str, Any]:
        """Return safe metadata, without protected defaults or supplied values."""
        controllers = {field.name: field for field in self.fields}
        rows = []
        for field in self.fields:
            status = (
                "conditional" if field.when else
                "defaulted" if field.has_default else
                "required" if field.required else "optional"
            )
            row: dict[str, Any] = {
                "name": field.name,
                "type": field.type,
                "description": field.description,
                "sitePath": ".".join(field.site_path),
                "required": field.required,
                "sensitive": field.sensitive,
                "status": status,
            }
            if field.has_default and not field.sensitive:
                row["default"] = field.default
            if field.when:
                row["when"] = {"input": field.when.input}
                if not controllers[field.when.input].sensitive:
                    row["when"]["equals"] = field.when.equals
            rows.append(row)
        return {"apiVersion": _VERSION, "kind": "SiteInputContract", "inputs": rows}

    def example(self) -> dict[str, Any]:
        """Provide incomplete answers, never a deployable placeholder Site."""
        return {
            "apiVersion": _VERSION,
            "kind": "SiteInputValues",
            "values": {
                field.name: None for field in self.fields
                if field.required and not field.has_default and field.when is None
            },
        }

    def resolve(
        self, values_file: Path | None = None, inline: list[str] | None = None,
    ) -> Site:
        """Merge declared defaults, file answers, then inline answers into one Site."""
        fields = {field.name: field for field in self.fields}
        inline_values: dict[str, str | bool] = {}
        for answer in inline or []:
            if not isinstance(answer, str) or "=" not in answer:
                raise GuidedInputError("Inline inputs must use NAME=VALUE format.")
            name, value = answer.split("=", 1)
            if name not in fields:
                raise GuidedInputError("Inline inputs contain an unknown input name.")
            if name in inline_values:
                raise GuidedInputError(f"Duplicate inline input '{name}'.")
            field = fields[name]
            if field.sensitive:
                raise GuidedInputError(f"Sensitive input '{name}' cannot be supplied inline.")
            if field.type == "boolean":
                if value not in ("true", "false"):
                    raise GuidedInputError(f"Input '{name}' must be a boolean (true or false).")
                inline_values[name] = value == "true"
            else:
                _typed_value(value, "string", f"Input '{name}'", required=field.required)
                inline_values[name] = value

        file_values: dict[str, Any] = {}
        if values_file is not None:
            document = _mapping(_read_yaml(values_file, label="Input values file"), "Input values file")
            _shape(
                document,
                allowed={"apiVersion", "kind", "values"},
                required={"apiVersion", "kind", "values"},
                label="Input values file",
            )
            if document["apiVersion"] != _VERSION:
                raise GuidedInputError("Input values file has an unsupported apiVersion.")
            if document["kind"] != "SiteInputValues":
                raise GuidedInputError("Input values file kind must be SiteInputValues.")
            file_values = _mapping(document["values"], "Input values")
            for name, value in file_values.items():
                if name not in fields:
                    raise GuidedInputError("Input values contain an unknown input name.")
                field = fields[name]
                if value is None and field.required:
                    if name not in inline_values:
                        raise GuidedInputError(f"Missing required input '{name}'.")
                    continue
                _typed_value(value, field.type, f"Input '{name}'", required=field.required)

        effective = {
            field.name: field.default for field in self.fields if field.has_default
        }
        effective.update(file_values)
        effective.update(inline_values)
        data = copy.deepcopy(self._site_defaults)
        active_values: dict[str, str | bool] = {}
        for field in self.fields:
            if field.when is not None:
                controller = active_values.get(field.when.input, _MISSING)
                if controller is _MISSING:
                    raise GuidedInputError(
                        f"Input '{field.when.input}' is missing; it controls conditional "
                        f"input '{field.name}'."
                    )
                if controller != field.when.equals:
                    if field.name in file_values or field.name in inline_values:
                        raise GuidedInputError(
                            f"Input '{field.name}' was supplied while its condition is inactive."
                        )
                    continue
            value = effective.get(field.name, _MISSING)
            if value is _MISSING:
                if field.required:
                    raise GuidedInputError(f"Missing required input '{field.name}'.")
                continue
            _assign(data, field.site_path, value)
            active_values[field.name] = value
        return Site.from_data(data, source="guided inputs", default_name="guided-site")


def _assign(data: dict[str, Any], path: tuple[str, ...], value: str | bool) -> None:
    current = data
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise GuidedInputError(
                f"Cannot map input to {'.'.join(path)}: '{part}' is not a mapping in siteDefaults."
            )
        current = child
    current[path[-1]] = value


def load_contract(
    manifest_path: Path, *, binding: MaterializedPackageBinding | None = None,
) -> InputContract | None:
    """Load this manifest's contract, if present; verify acquired bytes before reading."""
    path = contract_path(manifest_path)
    if binding is not None:
        binding.validate()
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    if binding is not None:
        verified, _ = binding.require_workspace_file(path)
    else:
        verified = path
    document = _mapping(_read_yaml(verified, label="Input contract"), "Input contract")
    if binding is not None:
        binding.require_workspace_file(path)
    _shape(
        document,
        allowed={"apiVersion", "kind", "siteDefaults", "inputs"},
        required={"apiVersion", "kind", "inputs"},
        label="Input contract",
    )
    if document["apiVersion"] != _VERSION:
        raise GuidedInputError("Input contract has an unsupported apiVersion.")
    if document["kind"] != "SiteInputContract":
        raise GuidedInputError("Input contract kind must be SiteInputContract.")
    defaults = _site_defaults(document.get("siteDefaults", {}))
    fields = _parse_fields(document["inputs"])
    _validate_default_writers(defaults, fields)
    return InputContract(fields, defaults)


def load_direct_site(path: Path) -> Site:
    """Read a bounded standalone Site, without access to configured inheritance."""
    data = _mapping(_read_yaml(path, label="Site file"), "Site file")
    if "inherits" in data or (
        isinstance(data.get("spec"), dict) and "inherits" in data["spec"]
    ):
        raise GuidedInputError("A direct Site file cannot use inherits; provide a complete Site.")
    if "apiVersion" in data and not isinstance(data["apiVersion"], str):
        raise GuidedInputError("Site file apiVersion must be text.")
    if data.get("kind") is not None and not isinstance(data["kind"], str):
        raise GuidedInputError("Site file kind must be text.")
    try:
        _validate_resource(data, "Site", path)
    except ValueError:
        raise GuidedInputError("Site file has an unsupported apiVersion or kind.") from None
    return Site.from_data(data, source=path, default_name=path.stem)


def write_yaml_exclusive(path: Path, data: dict[str, Any]) -> None:
    """Write UTF-8 YAML only to a new file in an existing directory."""
    _mapping(data, "YAML output")
    try:
        raw = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, yaml.YAMLError):
        raise GuidedInputError("YAML output must contain only supported plain data.") from None
    if len(raw) > _MAX_YAML_BYTES:
        raise GuidedInputError("YAML output exceeds the 128 KiB limit.")
    write_new(path, raw)
