"""The verified-bundle helper owns only its store and the named pipx application."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _wheel(path, version, *, app=True):
    prefix = f"siteops-{version}.dist-info"
    contents = {
        "siteops/__init__.py": f'__version__ = "{version}"\n'.encode(),
        "siteops/cli.py": (
            'from siteops import __version__\n'
            'def main():\n    print("siteops " + __version__)\n'
        ).encode(),
        prefix + "/METADATA": (
            f"Metadata-Version: 2.3\nName: siteops\nVersion: {version}\n"
            "Requires-Python: >=3.10\n"
        ).encode(),
        prefix + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if app:
        contents[prefix + "/entry_points.txt"] = b"[console_scripts]\nsiteops = siteops.cli:main\n"
    records = []
    for name, content in contents.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        records.append((name, "sha256=" + digest, len(content)))
    records.append((prefix + "/RECORD", "", ""))
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows(records)
    contents[prefix + "/RECORD"] = stream.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for name, content in contents.items():
            wheel.writestr(name, content)


@pytest.fixture
def bundle_factory(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from siteops_distribution import BundleManifest, BundleTarget, PayloadFile

    def create(number=1, *, app=True):
        root = tmp_path / f"bundle {number}"
        root.mkdir()
        (root / "wheels").mkdir()
        version = f"1.0.0b1+build.{number}.1.gaaaaaaaaaaaa"
        wheel_path = f"wheels/siteops-{version}-py3-none-any.whl"
        _wheel(root / wheel_path, version, app=app)
        shutil.copyfile(SCRIPTS / "install-siteops.py", root / "install.py")
        shutil.copyfile(SCRIPTS / "siteops_distribution.py", root / "siteops_distribution.py")
        for name in ("LICENSE", "ThirdPartyNotices.txt"):
            (root / name).write_text("Synthetic fixture notice.\n", encoding="utf-8")
        files = tuple(
            PayloadFile(
                path=path.relative_to(root).as_posix(),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size=path.stat().st_size,
            )
            for path in sorted(root.rglob("*")) if path.is_file()
        )
        target_platform = "windows-x86_64" if os.name == "nt" else "linux-x86_64"
        manifest = BundleManifest(
            version=version, base_version="1.0.0b1",
            repository="example/publisher", source_sha="a" * 40,
            source_ref="refs/heads/main", build_number=number, build_attempt=1,
            application_wheel=wheel_path,
            targets=(BundleTarget(
                python=f"{sys.version_info.major}.{sys.version_info.minor}",
                platform=target_platform, wheels=(wheel_path,),
            ),),
            files=files,
        )
        (root / "bundle.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return root, manifest

    return create


@pytest.fixture
def installer(bundle_factory, monkeypatch):
    root, manifest = bundle_factory()
    spec = importlib.util.spec_from_file_location("installer_under_test", root / "install.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, root, manifest


def test_store_retains_identical_payload_and_native_hash_links(installer, tmp_path):
    module, root, manifest = installer
    store = tmp_path / "private store"
    module._private_directory(store)
    retained = module.retain_bundle(root, manifest, store)

    assert retained != root
    assert module.manifest_digest(retained / "payload") == module.manifest_digest(root)
    module.verify_payload(retained / "payload", manifest)
    links = (retained / "wheel-links.html").read_text(encoding="utf-8")
    wheel = next(item for item in manifest.files if item.path == manifest.application_wheel)
    assert f"#sha256={wheel.sha256}" in links
    assert "payload/wheels/siteops-" in links
    assert module.retain_bundle(root, manifest, store) == retained
    assert not list(store.glob(".stage-*"))


def test_concurrent_retention_reuses_one_complete_entry(installer, tmp_path):
    module, root, manifest = installer
    store = tmp_path / "store"
    module._private_directory(store)
    with ThreadPoolExecutor(max_workers=2) as workers:
        paths = list(workers.map(lambda _: module.retain_bundle(root, manifest, store), range(2)))
    assert paths[0] == paths[1]
    module.verify_payload(paths[0] / "payload", manifest)
    assert not list(store.glob(".stage-*"))


def test_existing_store_is_not_silently_repaired(installer, tmp_path):
    module, root, manifest = installer
    store = tmp_path / "store"
    module._private_directory(store)
    retained = module.retain_bundle(root, manifest, store)
    links = retained / "wheel-links.html"
    links.write_text("operator-modified", encoding="utf-8")

    with pytest.raises(module.InstallError, match="wheel links"):
        module.retain_bundle(root, manifest, store)
    assert links.read_text(encoding="utf-8") == "operator-modified"


@pytest.mark.parametrize("variable", ["PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_TARGET"])
def test_child_environment_blocks_ambient_pip_policy(installer, tmp_path, monkeypatch, variable):
    module, _, _ = installer
    monkeypatch.setenv(variable, "untrusted-value")
    monkeypatch.setenv("PIPX_DEFAULT_BACKEND", "uv")
    monkeypatch.setenv("PIPX_FETCH_PYTHON", "always")
    monkeypatch.setenv("PIPX_DEFAULT_ENV_BACKEND", "uv")
    monkeypatch.setenv("PIPX_COOLDOWN", "7")
    environment = module._child_environment(tmp_path)
    assert variable not in environment
    assert environment["PIP_CONFIG_FILE"] == os.devnull
    assert environment["PIPX_DEFAULT_BACKEND"] == "pip"
    assert environment["PIPX_FETCH_PYTHON"] == "never"
    assert "PIPX_DEFAULT_ENV_BACKEND" not in environment
    assert "PIPX_COOLDOWN" not in environment
    assert Path(environment["PIP_CACHE_DIR"]).is_relative_to(tmp_path)


def test_invalid_bundle_does_not_invoke_pipx(installer, tmp_path, monkeypatch, capsys):
    module, root, manifest = installer
    (root / manifest.application_wheel).write_bytes(b"changed")
    monkeypatch.setattr(module.shutil, "which", lambda _: pytest.fail("No tool call for invalid content"))
    store = tmp_path / "store"
    assert module.main(["--output", "json", "--store-dir", str(store)]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["diagnostic"]["code"] == "invalid-bundle"
    assert not store.exists()


def test_storage_cannot_mutate_the_bundle(installer, monkeypatch, capsys):
    module, root, _ = installer
    monkeypatch.setattr(module.shutil, "which", lambda _: pytest.fail("Reject path before tools"))
    assert module.main(["--output", "json", "--store-dir", str(root / "state")]) == 1
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "invalid-store"
    assert not (root / "state").exists()


@pytest.mark.parametrize(
    "variable",
    ["PIPX_HOME", "PIPX_BIN_DIR", "PIPX_SHARED_LIBS", "XDG_STATE_HOME", "XDG_CACHE_HOME"],
)
def test_tool_data_cannot_mutate_the_bundle(installer, monkeypatch, capsys, tmp_path, variable):
    module, root, _ = installer
    monkeypatch.setenv(variable, str(root / "tool-data"))
    monkeypatch.setattr(module.shutil, "which", lambda _: pytest.fail("Reject path before tools"))
    assert module.main(["--output", "json", "--store-dir", str(tmp_path / "store")]) == 1
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "invalid-store"
    assert not (root / "tool-data").exists()


def test_relative_pipx_data_path_is_rejected(installer, monkeypatch, tmp_path):
    module, _, _ = installer
    monkeypatch.setenv("PIPX_HOME", "relative-pipx")
    with pytest.raises(module.InstallError, match="absolute"):
        module._child_environment(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory ownership contract.")
def test_store_refuses_writable_ancestor_before_creating_children(installer, tmp_path):
    module, _, _ = installer
    parent = tmp_path / "shared-parent"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    try:
        with pytest.raises(module.InstallError, match="parent directories"):
            module._private_directory(parent / "store")
        assert not (parent / "store").exists()
    finally:
        parent.chmod(0o700)


def test_store_rejects_link_ancestor_before_creating_children(installer, tmp_path):
    module, _, _ = installer
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a directory symlink requires host permission.")
    with pytest.raises(module.InstallError, match="regular files and directories"):
        module._private_directory(link / "store")
    assert not (target / "store").exists()


def test_reparse_directory_is_rejected(installer, monkeypatch, tmp_path):
    module, _, _ = installer
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400,
    ))
    with pytest.raises(module.InstallError, match="regular files and directories"):
        module._check_node(tmp_path, directory=True)


@pytest.fixture
def existing_client(installer, tmp_path):
    module, _, manifest = installer
    store = tmp_path / "client-store"
    module._private_directory(store)
    client = module.PipxClient("unused-pipx", store)
    client.venvs = tmp_path / "venvs"
    client.bin_dir = tmp_path / "commands"
    client.python = Path(sys._base_executable)
    slot, _, _ = client.paths()
    slot.mkdir(parents=True)
    metadata = {
        "main_package": {
            "package": "siteops", "package_version": manifest.version,
            "expected_apps": ["siteops"], "pinned": False,
        },
        "injected_packages": {}, "backend": "pip", "venv_args": [],
    }
    return client, metadata


@pytest.mark.parametrize(
    ("key", "value"),
    [("main_package", []), ("injected_packages", []), ("backend", "uv"),
     ("exposure_enabled", "true"), ("venv_args", "--system-site-packages")],
)
def test_invalid_existing_metadata_is_not_treated_as_absent(
    installer, existing_client, key, value,
):
    module, _, _ = installer
    client, metadata = existing_client
    metadata[key] = value
    slot, _, _ = client.paths()
    (slot / "pipx_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(module.InstallError):
        client.installed()


def test_missing_existing_metadata_is_an_actionable_failure(installer, existing_client):
    module, _, _ = installer
    client, _ = existing_client
    with pytest.raises(module.InstallError, match="no pipx metadata"):
        client.installed()


@pytest.mark.parametrize("policy", ["pinned", "injected", "locked", "unexposed", "system-packages"])
def test_custom_installation_policy_blocks_replacement(
    installer, existing_client, tmp_path, monkeypatch, capsys, policy,
):
    module, _, manifest = installer
    client, metadata = existing_client
    if policy == "pinned":
        metadata["main_package"]["pinned"] = True
        expected = "pinned-installation"
    elif policy == "injected":
        metadata["injected_packages"] = {"extra": {}}
        expected = "injected-packages"
    elif policy == "locked":
        metadata["main_package"]["lock_file"] = {"__type__": "Path", "__Path__": "private-lock"}
        expected = "locked-installation"
    else:
        metadata["exposure_enabled"] = policy != "unexposed"
        metadata["venv_args"] = ["--system-site-packages"] if policy == "system-packages" else []
        expected = "custom-environment"
    monkeypatch.setattr(module.shutil, "which", lambda _: "unused-pipx")
    monkeypatch.setattr(module, "PipxClient", lambda *_: client)
    monkeypatch.setattr(client, "prepare", lambda: None)
    monkeypatch.setattr(client, "installed", lambda: metadata)
    monkeypatch.setattr(client, "command", lambda *_args, **_kwargs: pytest.fail("No mutation"))
    target = manifest.targets[0]
    monkeypatch.setattr(module, "_platform_info", lambda *_: (target.python, target.platform))
    assert module.main(["--replace", "--output", "json", "--store-dir", str(tmp_path / "store")]) == 1
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == expected


def test_completed_uninstall_requires_removing_the_exposed_command(
    installer, existing_client, tmp_path, monkeypatch, capsys,
):
    module, _, _ = installer
    client, metadata = existing_client
    command = tmp_path / "remaining-command"
    command.write_bytes(b"unremoved")
    states = iter([metadata, None])
    monkeypatch.setattr(module.shutil, "which", lambda _: "unused-pipx")
    monkeypatch.setattr(module, "PipxClient", lambda *_: client)
    monkeypatch.setattr(client, "prepare", lambda: None)
    monkeypatch.setattr(client, "installed", lambda: next(states))
    monkeypatch.setattr(client, "command", lambda *_args, **_kwargs: "{}")
    monkeypatch.setattr(module, "_check_exposure", lambda *_args, **_kwargs: command)
    assert module.main(["--uninstall", "--output", "json", "--store-dir", str(tmp_path / "store")]) == 1
    assert json.loads(capsys.readouterr().out)["diagnostic"]["code"] == "removal-incomplete"
    assert command.read_bytes() == b"unremoved"


def test_redacted_failure_keeps_actionable_text_without_paths(installer, monkeypatch, capsys, tmp_path):
    module, _, _ = installer
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    assert module.main(["--output", "json", "--store-dir", str(tmp_path / "private-name")]) == 1
    output = capsys.readouterr().out
    assert "pipx is required" in output
    assert "private-name" not in output
    assert json.loads(output)["exitCode"] == 1


@pytest.mark.parametrize("marker", ["GITHUB_ACTIONS", "TF_BUILD"])
def test_ci_redaction_and_explicit_private_override(installer, monkeypatch, marker):
    module, _, _ = installer
    monkeypatch.delenv("SITEOPS_REDACT_OUTPUT")
    monkeypatch.setenv(marker, "true")
    assert module._redacted()
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    assert not module._redacted()


@pytest.mark.parametrize("mutation", [False, True])
def test_tool_calls_isolate_interrupts_and_bound_only_preflight(
    installer, existing_client, monkeypatch, mutation,
):
    module, _, _ = installer
    client, _ = existing_client
    calls = []

    def run(command, **options):
        calls.append(options)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="private tool detail")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert client.run(["unused-tool"], mutation=mutation) == "{}"
    assert calls[0]["timeout"] == (None if mutation else 30)
    if os.name == "nt":
        assert calls[0]["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert calls[0]["start_new_session"] is True
    logs = list(client.logs.glob("step-*.log"))
    assert len(logs) == 1
    assert "private tool detail" in logs[0].read_text(encoding="utf-8")


def test_stop_preserves_completed_result_and_restores_signal_handler(
    installer, monkeypatch, capsys,
):
    module, _, manifest = installer
    previous = signal.getsignal(signal.SIGINT)

    def completed(args, stop):
        signal.raise_signal(signal.SIGINT)
        assert stop.requested
        return {
            "apiVersion": "siteops.install/v1", "kind": "SiteOpsInstallationResult",
            "package": "siteops", "status": "installed", "version": manifest.version,
        }

    monkeypatch.setattr(module, "execute", completed)
    assert module.main(["--output", "json"]) == 130
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "installed"
    assert result["interrupted"] is True
    assert result["exitCode"] == 130
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.parametrize(
    "info",
    [
        {"implementation": "pypy", "bits": 64, "freeThreaded": False},
        {"implementation": "cpython", "bits": 32, "freeThreaded": False},
        {"implementation": "cpython", "bits": 64, "freeThreaded": True},
        {"implementation": "cpython", "bits": 64, "freeThreaded": False,
         "machine": "aarch64", "system": "Linux", "libc": "glibc"},
        {"implementation": "cpython", "bits": 64, "freeThreaded": False,
         "machine": "x86_64", "system": "Linux", "libc": "musl"},
        {"implementation": "cpython", "bits": 64, "freeThreaded": False,
         "machine": "x86_64", "system": "Linux", "libc": "glibc", "libcVersion": "2.16"},
    ],
)
def test_unsupported_python_is_rejected_before_install(installer, info, tmp_path):
    module, _, _ = installer
    client = SimpleNamespace(run=lambda _: json.dumps(info))
    with pytest.raises(module.InstallError):
        module._platform_info(client, tmp_path / "python")


@pytest.mark.parametrize("libc_version", ["2.17", "2.39"])
def test_supported_linux_runtime_is_accepted(installer, tmp_path, libc_version):
    module, _, _ = installer
    client = SimpleNamespace(run=lambda _: json.dumps({
        "implementation": "cpython", "bits": 64, "freeThreaded": False,
        "machine": "x86_64", "system": "Linux", "libc": "glibc",
        "libcVersion": libc_version, "python": "3.11",
    }))
    assert module._platform_info(client, tmp_path / "python") == ("3.11", "linux-x86_64")


def test_plain_rendering_supports_ascii_streams(installer, monkeypatch):
    module, _, _ = installer
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii")
    monkeypatch.setattr(module.sys, "stdout", stream)
    module._render({
        "status": "installed", "version": "1.0.0",
        "command": "C:\\operator-\u03b1\\siteops", "pathReady": True,
    }, "plain")
    stream.flush()
    assert b"siteops --help" in buffer.getvalue()
    assert b"\x1b" not in buffer.getvalue()


@pytest.mark.skipif(
    platform.system() not in {"Windows", "Linux"},
    reason="Native helper qualification covers Windows and Linux.",
)
def test_real_pipx_lifecycle_preserves_existing_app_on_failed_replacement(
    bundle_factory, tmp_path,
):
    root_a, manifest_a = bundle_factory(10)
    root_b, manifest_b = bundle_factory(11)
    root_bad, _ = bundle_factory(12, app=False)
    store = tmp_path / "retained data"
    environment = {**os.environ}
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.update({
        "PATH": str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", ""),
        "PIPX_HOME": str(tmp_path / "pipx"),
        "PIPX_BIN_DIR": str(tmp_path / "bin"),
        "PIPX_MAN_DIR": str(tmp_path / "man"),
        "PIPX_COMPLETION_DIR": str(tmp_path / "completions"),
        "PIPX_SHARED_LIBS": str(tmp_path / "shared"),
        "PIPX_DEFAULT_PYTHON": sys._base_executable,
        "SITEOPS_REDACT_OUTPUT": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    })

    def invoke(root, *arguments, expected=0):
        result = subprocess.run(
            [sys._base_executable, "-B", str(root / "install.py"), "--output", "json",
             "--store-dir", str(store), *arguments],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        document = json.loads(result.stdout)
        assert document["exitCode"] == expected
        assert "command" not in document
        return document

    assert invoke(root_a)["status"] == "installed"
    assert invoke(root_a)["status"] == "already-installed"
    assert invoke(root_b, expected=1)["diagnostic"]["code"] == "already-installed"
    assert invoke(root_b, "--reinstall", expected=1)["diagnostic"]["code"] == "build-mismatch"
    assert invoke(root_b, "--replace")["version"] == manifest_b.version
    assert invoke(root_bad, "--replace", expected=1)["diagnostic"]["code"] == "pipx-failed"
    assert invoke(root_b)["status"] == "already-installed"
    assert invoke(root_b, "--reinstall")["status"] == "reinstalled"
    assert invoke(root_a, "--replace")["version"] == manifest_a.version
    assert invoke(root_a, "--uninstall")["status"] == "removed"
    assert invoke(root_a, "--uninstall")["status"] == "not-installed"
    assert list(store.glob("*/payload/bundle.json"))
