# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Install an externally authenticated Site Ops bundle with managed pipx tooling."""

from __future__ import annotations

import argparse
import errno
import hashlib
import html
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, TextIO

sys.dont_write_bytecode = True

from siteops_distribution import (  # noqa: E402
    BundleManifest,
    DistributionError,
    load_manifest,
    manifest_digest,
    select_target,
    verify_payload,
)

_PYTHON_INFO = (
    "import json,platform,struct,sys,sysconfig;"
    "print(json.dumps({'python':str(sys.version_info.major)+'.'+str(sys.version_info.minor),"
    "'implementation':sys.implementation.name,'system':platform.system(),"
    "'machine':platform.machine(),'bits':struct.calcsize('P')*8,"
    "'freeThreaded':bool(sysconfig.get_config_var('Py_GIL_DISABLED')),"
    "'libc':platform.libc_ver()[0],'libcVersion':platform.libc_ver()[1]}))"
)
_MAX_METADATA = 1024 * 1024
_PIPX_DATA_VARIABLES = (
    "PIPX_HOME", "PIPX_BIN_DIR", "PIPX_MAN_DIR", "PIPX_COMPLETION_DIR", "PIPX_SHARED_LIBS",
)
_USER_DATA_VARIABLES = (
    "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
    "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
)


class InstallError(Exception):
    """An expected installation failure with a stable diagnostic category."""

    def __init__(self, code: str, message: str, log: Path | None = None):
        super().__init__(message)
        self.code = code
        self.log = log


@dataclass
class StopRequest:
    requested: bool = False


def _write(stream: TextIO, text: str) -> None:
    encoding = stream.encoding or "utf-8"
    stream.write(text.encode(encoding, errors="backslashreplace").decode(encoding))
    stream.flush()


def _progress(message: str) -> None:
    _write(sys.stderr, f"  {message}\n")


def _redacted() -> bool:
    value = os.environ.get("SITEOPS_REDACT_OUTPUT", "").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    return bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("TF_BUILD"))


def _plain_path(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise InstallError("invalid-path", "Installation paths must be absolute.")
    return Path(os.path.abspath(path))


def _store_root(selected: Path | None) -> Path:
    if selected is not None:
        return _plain_path(selected)
    if os.name == "nt":
        parent = os.environ.get("LOCALAPPDATA")
        if not parent:
            raise InstallError("missing-data-path", "Set --store-dir to a private absolute path.")
        return _plain_path(Path(parent)) / "siteops" / "installations"
    parent = os.environ.get("XDG_DATA_HOME")
    return (
        _plain_path(Path(parent)) if parent else Path.home() / ".local" / "share"
    ) / "siteops" / "installations"


def _check_node(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0,
    )
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if reparse or stat.S_ISLNK(info.st_mode) or not valid:
        raise InstallError("unsafe-path", "Installation data must use regular files and directories.")


def _private_directory(path: Path) -> None:
    for parent in (*reversed(path.parents), path):
        parent.mkdir(exist_ok=True, mode=0o700)
        _check_node(parent, directory=True)
        if os.name == "posix":
            info = parent.lstat()
            trusted_sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in {0, os.geteuid()} or (
                info.st_mode & 0o022 and not trusted_sticky_root
            ):
                raise InstallError(
                    "unsafe-store",
                    "Choose private installation storage whose parent directories are trusted.",
                )
    _check_owned(path)


def _check_owned(path: Path) -> None:
    if os.name == "posix":
        info = path.lstat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise InstallError(
                "unsafe-store",
                "Installation storage must be owned by you and not writable by other users.",
            )


def _private_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _read_json(path: Path) -> dict[str, Any]:
    _check_node(path, directory=False)
    with path.open("rb") as stream:
        raw = stream.read(_MAX_METADATA + 1)
    if len(raw) > _MAX_METADATA:
        raise InstallError("invalid-metadata", "Installation metadata is too large.")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise InstallError("invalid-metadata", "Installation metadata is not valid JSON.") from error
    if not isinstance(document, dict):
        raise InstallError("invalid-metadata", "Installation metadata must be an object.")
    return document


def _wheel_links(manifest: BundleManifest) -> bytes:
    lines = ["<!doctype html>", "<html><body>"]
    for entry in sorted(manifest.files, key=lambda item: item.path):
        if entry.path.endswith(".whl"):
            url = "payload/" + urllib.parse.quote(entry.path, safe="/")
            lines.append(
                f'<a href="{url}#sha256={entry.sha256}">{html.escape(entry.path)}</a>'
            )
    lines.append("</body></html>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _verify_stored(path: Path, manifest: BundleManifest, identity: str) -> None:
    _check_node(path, directory=True)
    _check_owned(path)
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in (*directories, *files):
            child = Path(parent) / name
            _check_node(child, directory=name in directories)
            _check_owned(child)
    if {item.name for item in path.iterdir()} != {"payload", "wheel-links.html"}:
        raise InstallError("invalid-store", "Stored installation data has unexpected entries.")
    stored = load_manifest(path / "payload")
    if manifest_digest(path / "payload") != identity or stored != manifest:
        raise InstallError("invalid-store", "Stored installation data does not match this bundle.")
    verify_payload(path / "payload", stored)
    _check_node(path / "wheel-links.html", directory=False)
    if (path / "wheel-links.html").read_bytes() != _wheel_links(manifest):
        raise InstallError("invalid-store", "Stored wheel links do not match the bundle.")


def retain_bundle(root: Path, manifest: BundleManifest, store: Path) -> Path:
    """Retain verified content without making pipx depend on temporary extraction."""
    identity = manifest_digest(root)
    destination = store / identity
    if destination.exists() or destination.is_symlink():
        _verify_stored(destination, manifest, identity)
        return destination
    staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=store))
    try:
        payload = staging / "payload"
        _private_directory(payload)
        for entry in manifest.files:
            source = root / entry.path
            _check_node(source, directory=False)
            target = payload / entry.path
            _private_directory(target.parent)
            with source.open("rb") as stream:
                content = stream.read(entry.size + 1)
            if len(content) != entry.size or hashlib.sha256(content).hexdigest() != entry.sha256:
                raise InstallError("bundle-changed", "Bundle content changed while being retained.")
            _private_file(target, content)
        _check_node(root / "bundle.json", directory=False)
        with (root / "bundle.json").open("rb") as stream:
            document = stream.read(_MAX_METADATA + 1)
        if len(document) > _MAX_METADATA or hashlib.sha256(document).hexdigest() != identity:
            raise InstallError("bundle-changed", "Bundle metadata changed while being retained.")
        _private_file(payload / "bundle.json", document)
        _private_file(staging / "wheel-links.html", _wheel_links(manifest))
        _verify_stored(staging, manifest, identity)
        try:
            staging.rename(destination)
        except OSError as error:
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            _verify_stored(destination, manifest, identity)
        return destination
    finally:
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError:
                detail = "" if _redacted() else f" {staging}"
                _progress(f"Warning: unused installation staging data could not be removed.{detail}")


def _child_environment(store: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("PIP_") and key not in {"PYTHONPATH", "PYTHONHOME"}
        and (not key.startswith("PIPX_") or key in {*_PIPX_DATA_VARIABLES, "PIPX_DEFAULT_PYTHON"})
    }
    environment.update({
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_CACHE_DIR": str(store / "pip-cache"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_KEYRING_PROVIDER": "disabled",
        "PIPX_DEFAULT_BACKEND": "pip",
        "PIPX_FETCH_PYTHON": "never",
        "PIPX_USE_EMOJI": "0",
        "PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for name in _PIPX_DATA_VARIABLES:
        if environment.get(name):
            environment[name] = str(_plain_path(Path(environment[name])))
    if not environment.get("PIPX_DEFAULT_PYTHON"):
        base_python = getattr(sys, "_base_executable", None)
        if not base_python or not Path(base_python).is_absolute():
            raise InstallError("python-unavailable", "The base Python interpreter could not be located.")
        environment["PIPX_DEFAULT_PYTHON"] = base_python
    return environment


class PipxClient:
    """Use the supported pipx CLI without importing or changing its internals."""

    def __init__(self, executable: str, store: Path):
        self.executable = executable
        self.store = store
        self.environment = _child_environment(store)
        self.logs = store / "logs"
        _private_directory(self.logs)
        self.venvs: Path | None = None
        self.bin_dir: Path | None = None
        self.python: Path | None = None

    def command(
        self, arguments: list[str], *, mutation: bool = False,
    ) -> str:
        return self.run([self.executable, *arguments], mutation=mutation)

    def run(self, command: list[str], *, mutation: bool = False) -> str:
        # Mutating pipx calls must finish their own preservation/rollback path.
        # A separate process group keeps our cooperative Ctrl-C from killing it.
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        try:
            result = subprocess.run(
                command, cwd=self.store, env=self.environment,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=None if mutation else 30, **options,
            )
        except subprocess.TimeoutExpired as error:
            raise InstallError("tool-timeout", "An installation preflight command timed out.") from error
        log = self.logs / f"step-{uuid.uuid4().hex}.log"
        _private_file(log, (result.stdout + result.stderr).encode("utf-8"))
        if result.returncode != 0:
            raise InstallError(
                "pipx-failed",
                "The installation command failed. Inspect private diagnostics before retrying.",
                log,
            )
        return result.stdout

    def prepare(self) -> None:
        version = self.command(["--version"]).strip()
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (1, 17, 2) or match[1] != "1":
            raise InstallError(
                "pipx-version", "Install a supported pipx 1.x release, version 1.17.2 or newer.",
            )
        values = {}
        for name in ("PIPX_LOCAL_VENVS", "PIPX_BIN_DIR", "PIPX_DEFAULT_PYTHON"):
            value = self.command(["environment", "--value", name]).strip()
            if not value or "\n" in value or "\r" in value:
                raise InstallError("pipx-path", "pipx did not return a usable installation path.")
            values[name] = _plain_path(Path(value))
        self.venvs = values["PIPX_LOCAL_VENVS"]
        self.bin_dir = values["PIPX_BIN_DIR"]
        self.python = values["PIPX_DEFAULT_PYTHON"]

    def paths(self) -> tuple[Path, Path, Path]:
        if self.venvs is None or self.bin_dir is None or self.python is None:
            raise InstallError("pipx-path", "pipx installation paths have not been prepared.")
        return self.venvs / "siteops", self.bin_dir, self.python

    def installed(self) -> dict[str, Any] | None:
        slot, _, _ = self.paths()
        if not slot.exists() and not slot.is_symlink():
            return None
        _check_node(slot, directory=True)
        try:
            metadata = _read_json(slot / "pipx_metadata.json")
        except FileNotFoundError as error:
            raise InstallError(
                "pipx-metadata",
                "The existing Site Ops environment has no pipx metadata. Inspect it before retrying.",
            ) from error
        package = metadata.get("main_package")
        if not isinstance(package, dict) or package.get("package") != "siteops":
            raise InstallError("pipx-metadata", "The existing pipx environment is not a valid Site Ops installation.")
        if not isinstance(package.get("package_version"), str):
            raise InstallError("pipx-metadata", "The installed Site Ops version could not be determined.")
        expected_apps = package.get("expected_apps", [])
        if (
            not isinstance(expected_apps, list)
            or any(not isinstance(app, str) for app in expected_apps)
            or type(package.get("pinned", False)) is not bool
            or not isinstance(metadata.get("injected_packages", {}), dict)
            or type(metadata.get("exposure_enabled", True)) is not bool
            or not isinstance(metadata.get("venv_args", []), list)
            or any(not isinstance(value, str) for value in metadata.get("venv_args", []))
        ):
            raise InstallError("pipx-metadata", "The existing pipx metadata has an unsupported shape.")
        if metadata.get("backend") not in {None, "pip"}:
            raise InstallError("pipx-backend", "This installer manages pip-backed Site Ops environments only.")
        return metadata


def _python_in(slot: Path) -> Path:
    return slot / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _platform_info(client: PipxClient, python: Path) -> tuple[str, str]:
    try:
        info = json.loads(client.run([str(python), "-I", "-S", "-c", _PYTHON_INFO]))
    except (ValueError, TypeError) as error:
        raise InstallError("python-unavailable", "Python did not return valid platform information.") from error
    if not isinstance(info, dict) or info.get("implementation") != "cpython":
        raise InstallError("unsupported-python", "This bundle requires a supported CPython interpreter.")
    if info.get("bits") != 64 or info.get("freeThreaded"):
        raise InstallError("unsupported-python", "This bundle requires a standard 64-bit CPython build.")
    machine = str(info.get("machine", "")).lower()
    system = info.get("system")
    if machine not in {"amd64", "x86_64"} or system not in {"Windows", "Linux"}:
        raise InstallError("unsupported-platform", "This bundle supports Windows x64 and Linux glibc x64.")
    if system == "Linux" and info.get("libc") != "glibc":
        raise InstallError("unsupported-platform", "This bundle requires glibc on Linux.")
    if system == "Linux":
        libc = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)*", str(info.get("libcVersion", "")))
        if libc is None or tuple(map(int, libc.groups())) < (2, 17):
            raise InstallError("unsupported-platform", "This bundle requires glibc 2.17 or newer.")
    python_version = info.get("python")
    if not isinstance(python_version, str) or not re.fullmatch(r"3\.\d+", python_version):
        raise InstallError("unsupported-python", "Python did not report a supported version.")
    return python_version, "windows-x86_64" if system == "Windows" else "linux-x86_64"


def _same_build(metadata: dict[str, Any], manifest: BundleManifest, digest: str) -> bool:
    package = metadata["main_package"]
    spec = package.get("package_or_url")
    if not isinstance(spec, str):
        return False
    name, separator, location = spec.partition("@")
    return (
        bool(separator) and name.strip() == "siteops"
        and urllib.parse.urlsplit(location.strip()).scheme == "file"
        and urllib.parse.urlsplit(location.strip()).fragment == f"sha256={digest}"
        and package["package_version"] == manifest.version
        and "siteops" in package.get("expected_apps", [])
    )


def _check_exposure(client: PipxClient, *, exists: bool) -> Path:
    slot, bin_dir, _ = client.paths()
    name = "siteops.exe" if os.name == "nt" else "siteops"
    command = bin_dir / name
    if not command.exists() and not command.is_symlink():
        return command
    expected = slot / ("Scripts" if os.name == "nt" else "bin") / name
    if not exists or not expected.is_file():
        raise InstallError("command-conflict", "A Site Ops command already exists outside the managed environment.")
    same = command.resolve() == expected.resolve()
    if not same and command.is_file() and command.stat().st_size == expected.stat().st_size:
        same = hashlib.sha256(command.read_bytes()).digest() == hashlib.sha256(expected.read_bytes()).digest()
    if not same:
        raise InstallError("command-conflict", "The exposed command differs from pipx's Site Ops command.")
    return command


def _check_stop(stop: StopRequest) -> None:
    if stop.requested:
        raise InstallError("interrupted", "Stopped before starting another installation change.")


def execute(args: argparse.Namespace, stop: StopRequest) -> dict[str, Any]:
    root = Path(__file__).absolute().parent
    manifest = None
    if not args.uninstall:
        _progress("Checking bundle contents...")
        manifest = load_manifest(root)
        verify_payload(root, manifest)
    store = _store_root(args.store_dir)
    if store.resolve().is_relative_to(root.resolve()):
        raise InstallError("invalid-store", "Choose installation storage outside the extracted bundle.")
    for name in (*_PIPX_DATA_VARIABLES, *_USER_DATA_VARIABLES):
        selected = os.environ.get(name)
        if selected and Path(selected).expanduser().absolute().resolve().is_relative_to(root.resolve()):
            raise InstallError("invalid-store", "pipx data paths must stay outside the extracted bundle.")
    _private_directory(store)
    executable = shutil.which("pipx")
    if not executable:
        raise InstallError("pipx-missing", "pipx is required. Install supported pipx tooling, then try again.")
    _progress("Checking managed tooling...")
    client = PipxClient(executable, store)
    client.prepare()
    metadata = client.installed()
    slot, _, default_python = client.paths()
    if Path(sys.prefix).resolve().is_relative_to(slot.resolve()):
        raise InstallError("active-environment", "Run the installer outside the Site Ops environment being changed.")
    command = _check_exposure(client, exists=metadata is not None)
    result: dict[str, Any] = {
        "apiVersion": "siteops.install/v1", "kind": "SiteOpsInstallationResult",
        "package": "siteops", "status": "not-installed",
        "version": metadata["main_package"]["package_version"] if metadata else None,
    }
    if args.uninstall:
        if metadata is not None:
            _check_stop(stop)
            _progress("Removing Site Ops. Retained bundles will be kept...")
            client.command(["uninstall", "siteops", "--output", "json"], mutation=True)
            if client.installed() is not None or command.exists() or command.is_symlink():
                raise InstallError(
                    "removal-incomplete",
                    "The Site Ops environment or exposed command remains after removal.",
                )
            result["status"] = "removed"
        return result

    if manifest is None:
        raise InstallError("invalid-bundle", "An installation requires a complete bundle.")
    python = _python_in(slot) if metadata is not None else default_python
    python_version, platform_name = _platform_info(client, python)
    select_target(manifest, python_version, platform_name)
    app = next(item for item in manifest.files if item.path == manifest.application_wheel)
    same = metadata is not None and _same_build(metadata, manifest, app.sha256)
    if args.reinstall and metadata is not None and not same:
        raise InstallError("build-mismatch", "Use --replace, not --reinstall, to select a different build.")
    if metadata is not None and not same and not args.replace:
        raise InstallError(
            "already-installed",
            "A different Site Ops build is installed. Use --replace to select this build deliberately.",
        )
    if metadata is not None and (args.replace or args.reinstall):
        if metadata["main_package"].get("pinned"):
            raise InstallError("pinned-installation", "Site Ops is pinned in pipx. Unpin it before changing the build.")
        if metadata.get("injected_packages"):
            raise InstallError("injected-packages", "Remove injected packages before changing this managed installation.")
        if metadata["main_package"].get("lock_file") is not None:
            raise InstallError(
                "locked-installation",
                "The existing installation uses a separate pipx lock file. Migrate it explicitly.",
            )
        if not metadata.get("exposure_enabled", True) or "--system-site-packages" in metadata.get(
            "venv_args", [],
        ):
            raise InstallError(
                "custom-environment",
                "This installer requires an isolated pipx environment with command exposure enabled.",
            )
    _check_stop(stop)
    _progress(f"Retaining build {manifest.version}...")
    retained = retain_bundle(root, manifest, store)
    if not same or args.reinstall:
        _check_stop(stop)
        _progress("Installing with pipx. Ctrl-C waits for this step to finish...")
        wheel = retained / "payload" / manifest.application_wheel
        spec = f"siteops @ {wheel.as_uri()}#sha256={app.sha256}"
        pip_args = (
            "--no-index --only-binary=:all: --disable-pip-version-check "
            "--find-links=" + (retained / "wheel-links.html").as_uri()
        )
        arguments = [
            "install", spec, "--backend", "pip", "--fetch-python", "never",
            "--skip-maintenance", "--app", "siteops", "--pip-args", pip_args,
            "--output", "json",
        ]
        if metadata is not None:
            arguments.append("--force")
        else:
            arguments.extend(["--python", str(default_python)])
        client.command(arguments, mutation=True)
    _progress("Confirming the installed build...")
    installed = client.installed()
    if installed is None or not _same_build(installed, manifest, app.sha256):
        raise InstallError("installation-incomplete", "pipx did not install the selected build.")
    command = _check_exposure(client, exists=True)
    if not command.is_file():
        raise InstallError("command-missing", "pipx did not expose the Site Ops command.")
    version = client.run([str(command), "--version"]).strip()
    if version != f"siteops {manifest.version}":
        raise InstallError("version-mismatch", "The installed command did not report the selected build.")
    result.update({
        "status": "reinstalled" if args.reinstall and same else "already-installed" if same
        else "replaced" if metadata is not None else "installed",
        "version": manifest.version,
    })
    if not _redacted():
        found = shutil.which("siteops")
        result.update({
            "sourceRepository": manifest.repository, "sourceCommit": manifest.source_sha,
            "store": str(retained), "command": str(command),
            "pathReady": bool(found and Path(found).resolve() == command.resolve()),
        })
    return result


def _render(result: dict[str, Any], output: str) -> None:
    if output == "json":
        _write(sys.stdout, json.dumps(result, indent=2, sort_keys=True) + "\n")
        return
    _write(sys.stdout, f"\n  Site Ops: {result['status'].replace('-', ' ')}\n")
    if result.get("version"):
        _write(sys.stdout, f"  Version: {result['version']}\n")
    if result.get("sourceRepository") and result.get("sourceCommit"):
        _write(sys.stdout, f"  Source: {result['sourceRepository']}@{result['sourceCommit']}\n")
    if result.get("store"):
        _write(sys.stdout, f"  Data: {result['store']}\n")
    if result.get("command"):
        _write(sys.stdout, f"  Command: {result['command']}\n")
    if result.get("diagnostic"):
        _write(sys.stdout, f"  {result['diagnostic']['message']}\n")
        if result["diagnostic"].get("log"):
            _write(sys.stdout, f"  Details: {result['diagnostic']['log']}\n")
    elif result["status"] not in {"removed", "not-installed"}:
        if result.get("pathReady") is False:
            _write(sys.stdout, "  Next: add the pipx command directory to PATH, then open a new terminal.\n")
        else:
            _write(sys.stdout, "  Next: siteops --help\n")
    if result.get("interrupted"):
        _write(sys.stdout, "  Stop requested. The result includes any changes that completed.\n")
    _write(sys.stdout, "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install a Site Ops bundle already authenticated with the published "
            "verification instructions. This helper checks contents, not publisher signatures."
        ),
    )
    parser.add_argument("--store-dir", type=Path, help="Private absolute directory for retained installation data.")
    parser.add_argument("--output", choices=("plain", "json"), default="plain")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--replace", action="store_true", help="Replace a different installed build deliberately.")
    operations.add_argument("--reinstall", action="store_true", help="Repair the same selected build.")
    operations.add_argument("--uninstall", action="store_true", help="Remove Site Ops, retaining downloaded bundles.")
    args = parser.parse_args(argv)
    stop = StopRequest()
    previous = signal.getsignal(signal.SIGINT)

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.requested = True
        _progress("Stop requested. Waiting for the current pipx step before returning.")

    signal.signal(signal.SIGINT, request_stop)
    try:
        try:
            result = execute(args, stop)
            exit_code = 130 if stop.requested else 0
        except (InstallError, DistributionError, OSError) as error:
            exit_code = 130 if stop.requested else 1
            code = error.code if isinstance(error, InstallError) else "invalid-bundle" if isinstance(
                error, DistributionError,
            ) else "filesystem-error"
            message = str(error) if isinstance(error, InstallError) or not _redacted() else {
                "invalid-bundle": "Bundle contents are invalid. Use a verified, complete bundle.",
                "filesystem-error": "Installation data could not be accessed.",
            }.get(code, "Installation could not complete. Review private diagnostics and prerequisites.")
            result = {
                "apiVersion": "siteops.install/v1", "kind": "SiteOpsInstallationResult",
                "package": "siteops", "status": "failed", "version": None,
                "diagnostic": {"code": code, "message": message},
            }
            if isinstance(error, InstallError) and error.log is not None and not _redacted():
                result["diagnostic"]["log"] = str(error.log)
        interrupted = stop.requested
        exit_code = 130 if interrupted else exit_code
        result["interrupted"] = interrupted
        result["exitCode"] = exit_code
        _render(result, args.output)
        return exit_code
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
