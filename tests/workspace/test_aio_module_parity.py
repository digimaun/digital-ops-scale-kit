"""Per-API-version Bicep module parity tests for the AIO upgrade flow.

Per-version modules (e.g. `instance-2026-03-01.bicep`, `instance-2025-10-01.bicep`)
are typically copied from the previous version and minimally diverged when a new
API version ships. Unintentional drift (a fix applied to one version but not
mirrored) is a real risk as the module count grows.

These tests enforce the public-surface contract: param names, output names,
and exported function names must match across versions of the same module
family. Schema-level differences (resource bodies, internal helpers) are
expected and out of scope here.
"""

import re
from pathlib import Path

import pytest

WORKSPACE_PATH = (
    Path(__file__).parent.parent.parent
    / "workspaces"
    / "iot-operations"
)
AIO_MODULES_DIR = WORKSPACE_PATH / "templates" / "aio" / "modules"
DEPS_MODULES_DIR = WORKSPACE_PATH / "templates" / "deps" / "modules"

# Names a generation introduces and every later generation carries, keyed by
# module family, then surface kind, then the version that first declares them.
# Module filenames end in the API version, which is a date, so they compare
# lexicographically and a later generation inherits the name without an edit.
#
# Requiring such a name everywhere would add a resource arm to generations that
# cannot use it, so the parity rule carries this exception instead. It stays
# narrow: the name must appear in exactly the modules of its family from its
# generation on.
GENERATION_SCOPED_NAMES: dict[str, dict[str, dict[str, str]]] = {
    "instance": {
        "params": {
            # `akriConnectorTemplates` arrived with the 2026-07-01 generation.
            # Earlier ones ship the statically deployed connector.
            "opcuaConnectorVersion": "2026-07-01",
        },
    },
}

# The API version a per-version module targets, taken from its filename.
_MODULE_VERSION = re.compile(r"-(\d{4}-\d{2}-\d{2})\.bicep$")


def _module_version(path: Path) -> str:
    """The API version in a module filename, or empty when it carries none."""
    match = _MODULE_VERSION.search(path.name)
    return match.group(1) if match else ""


def _module_family(path: Path) -> str:
    """The module family, which is the filename without its API version."""
    return _MODULE_VERSION.sub("", path.name)

# Matches a Bicep top-level declaration. Captures the kind and the name.
# Allows optional decorators on the previous line via re.MULTILINE on the
# anchored ^.
_BICEP_PARAM_PATTERN = re.compile(r"^param\s+([A-Za-z_]\w*)\b", re.MULTILINE)
_BICEP_OUTPUT_PATTERN = re.compile(r"^output\s+([A-Za-z_]\w*)\b", re.MULTILINE)
_BICEP_FUNC_PATTERN = re.compile(r"^func\s+([A-Za-z_]\w*)\b", re.MULTILINE)


def _extract_surface(path: Path) -> dict[str, set[str]]:
    """Return the public surface of a Bicep module: param, output, and func
    declarations. Comments and resource bodies are intentionally excluded.
    """
    text = path.read_text(encoding="utf-8")
    return {
        "params": set(_BICEP_PARAM_PATTERN.findall(text)),
        "outputs": set(_BICEP_OUTPUT_PATTERN.findall(text)),
        "funcs": set(_BICEP_FUNC_PATTERN.findall(text)),
    }


def _paired_modules(directory: Path, prefix: str) -> list[Path]:
    """Return all `<prefix>-<api-version>.bicep` modules in directory, sorted."""
    return sorted(directory.glob(f"{prefix}-*.bicep"))


def _assert_surface_parity(
    modules: list[Path],
    scoped: dict[str, dict[str, dict[str, str]]] | None = None,
) -> None:
    """Assert all modules share the same param, output, and func name sets.

    Uses union-vs-symmetric-difference rather than pairwise-against-baseline:
    when 3+ modules are compared and a single one is the outlier, this
    surfaces the outlier directly instead of forcing the reader to infer
    which side of N-1 baseline failures is the actual drift.

    Names listed in `scoped` for this module family are exempt from the
    shared-surface check and are instead required from their generation
    onward, so a generation-specific name cannot spread backward or vanish
    unnoticed.
    """
    if len(modules) < 2:
        pytest.skip(f"Need 2+ modules to compare, got {len(modules)}")
    scoped = GENERATION_SCOPED_NAMES if scoped is None else scoped
    surfaces = {m: _extract_surface(m) for m in modules}
    families = {_module_family(m) for m in modules}
    assert len(families) == 1, (
        f"Compared modules span more than one family: {sorted(families)}. "
        f"The scoped-name exception is keyed by family, so a mixed list would "
        f"apply the wrong exceptions."
    )
    by_kind = scoped.get(families.pop(), {})

    for kind in ("params", "outputs", "funcs"):
        exceptions = by_kind.get(kind, {})

        # A scoped name must appear in exactly the generations from the one
        # that introduced it onward, so a copy into an older module fails, and
        # so does dropping it from a generation that needs it. An empty
        # expected set is still asserted, because a name introduced after every
        # module present must appear in none of them.
        for name, introduced in exceptions.items():
            expected = {m.name for m in modules if _module_version(m) >= introduced}
            actual = {m.name for m, s in surfaces.items() if name in s[kind]}
            assert actual == expected, (
                f"Generation-scoped {kind[:-1]} '{name}' is declared by "
                f"{sorted(actual)}, expected exactly {sorted(expected)}, the "
                f"modules for API {introduced} or newer.\n"
                "Update GENERATION_SCOPED_NAMES if the scope changed."
            )

        union: set[str] = set().union(*(s[kind] for s in surfaces.values()))
        union -= set(exceptions)
        outliers: dict[str, set[str]] = {}
        for m, surface in surfaces.items():
            missing = union - surface[kind]
            if missing:
                outliers[m.name] = missing
        assert not outliers, (
            f"Module surface drift ({kind}) across {[m.name for m in modules]}.\n"
            f"Union of all names: {sorted(union)}\n"
            f"Missing per module:\n"
            + "\n".join(
                f"  {name}: missing {sorted(missing)}"
                for name, missing in outliers.items()
            )
        )


class TestAioInstanceModuleParity:
    """The `instance-<api-version>.bicep` modules under templates/aio/modules/
    must expose the same parameters, outputs, and exported funcs across
    versions. Resource bodies and per-version schema details are out of scope."""

    def test_instance_modules_share_surface(self):
        _assert_surface_parity(_paired_modules(AIO_MODULES_DIR, "instance"))

    def test_resolve_instance_modules_share_surface(self):
        _assert_surface_parity(_paired_modules(AIO_MODULES_DIR, "resolve-instance"))

    def test_update_instance_modules_share_surface(self):
        _assert_surface_parity(_paired_modules(AIO_MODULES_DIR, "update-instance"))


class TestAdrNamespaceModuleParity:
    """The `adr-ns-<api-version>.bicep` modules under templates/deps/modules/
    must expose the same parameters, outputs, and exported funcs across
    versions."""

    def test_adr_ns_modules_share_surface(self):
        _assert_surface_parity(_paired_modules(DEPS_MODULES_DIR, "adr-ns"))


class TestGenerationScopedNames:
    """The scoped-name exception narrows the parity rule without removing it.

    A scoped name is required from the generation that introduced it onward,
    so the cases below cover what the exception must still reject.
    """

    SCOPED = {"instance": {"params": {"newerOnly": "2026-07-01"}}}
    OLDER = "instance-2026-03-01.bicep"
    INTRODUCED = "instance-2026-07-01.bicep"
    NEWER = "instance-2027-01-01.bicep"

    @staticmethod
    def _modules(tmp_path: Path, declared: dict[str, list[str]]) -> list[Path]:
        paths = []
        for name, params in declared.items():
            path = tmp_path / name
            path.write_text(
                "".join(f"param {p} string\n" for p in params), encoding="utf-8"
            )
            paths.append(path)
        return paths

    def test_scoped_name_from_its_generation_onward_passes(self, tmp_path):
        modules = self._modules(
            tmp_path,
            {
                self.OLDER: ["shared"],
                self.INTRODUCED: ["shared", "newerOnly"],
                self.NEWER: ["shared", "newerOnly"],
            },
        )
        _assert_surface_parity(modules, scoped=self.SCOPED)

    def test_scoped_name_in_an_older_generation_fails(self, tmp_path):
        """Reaching back into a generation that cannot use it is drift."""
        modules = self._modules(
            tmp_path,
            {
                self.OLDER: ["shared", "newerOnly"],
                self.INTRODUCED: ["shared", "newerOnly"],
            },
        )
        with pytest.raises(AssertionError, match="Generation-scoped param 'newerOnly'"):
            _assert_surface_parity(modules, scoped=self.SCOPED)

    def test_scoped_name_missing_from_the_generation_that_introduced_it_fails(
        self, tmp_path
    ):
        modules = self._modules(
            tmp_path, {self.OLDER: ["shared"], self.INTRODUCED: ["shared"]}
        )
        with pytest.raises(AssertionError, match="Generation-scoped param 'newerOnly'"):
            _assert_surface_parity(modules, scoped=self.SCOPED)

    def test_a_newer_generation_must_also_declare_it(self, tmp_path):
        """A generation added later inherits the name rather than dropping it."""
        modules = self._modules(
            tmp_path,
            {
                self.INTRODUCED: ["shared", "newerOnly"],
                self.NEWER: ["shared"],
            },
        )
        with pytest.raises(AssertionError, match="Generation-scoped param 'newerOnly'"):
            _assert_surface_parity(modules, scoped=self.SCOPED)

    def test_a_name_introduced_after_every_module_must_appear_in_none(self, tmp_path):
        """A name scoped to a future generation is rejected everywhere.

        Covers the fail-open case: an empty expected set is still asserted, so
        an early declaration cannot slip through by also being exempt from the
        shared-surface check.
        """
        scoped = {"instance": {"params": {"futureOnly": "2099-01-01"}}}
        modules = self._modules(
            tmp_path,
            {self.OLDER: ["shared", "futureOnly"], self.INTRODUCED: ["shared"]},
        )
        with pytest.raises(AssertionError, match="Generation-scoped param 'futureOnly'"):
            _assert_surface_parity(modules, scoped=scoped)

    def test_an_unscoped_name_missing_from_one_module_still_fails(self, tmp_path):
        """The original rule still applies to every name outside the exception.

        The scoped name is present and correct here, so the failure can only
        come from the shared-surface check.
        """
        modules = self._modules(
            tmp_path,
            {
                self.OLDER: ["shared"],
                self.INTRODUCED: ["shared", "newerOnly", "unlisted"],
            },
        )
        with pytest.raises(AssertionError, match="Module surface drift"):
            _assert_surface_parity(modules, scoped=self.SCOPED)
