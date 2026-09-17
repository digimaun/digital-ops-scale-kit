# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Install the selected authenticated engine and probe its workspace consumption."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_verification import ReleaseVerifier  # noqa: E402
from siteops_distribution import select_target  # noqa: E402
from siteops_release_assets import (  # noqa: E402
    ARCHIVE_NAME,
    PROOF_SUFFIX,
    FrozenReleaseAssets,
    native_engine_wheel,
)
from workspace_engine import SELECTION_NAME, EngineSelection, extract_engine_bundle  # noqa: E402

from siteops.artifacts import ArtifactError, hash_file  # noqa: E402
from siteops.process_capture import BoundedCapture  # noqa: E402
from siteops.workspace_source import (  # noqa: E402
    WORKSPACE_RELEASE_NAME,
    ArtifactIdentity,
    WorkspaceReleaseAssets,
)


def application_environment(root: Path, python: Path, gh: Path) -> dict[str, str]:
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_ARCHITEW6432",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    for name in ("home", "temp", "azure", "github", "appdata", "localappdata"):
        (root / name).mkdir(mode=0o700)
    environment.update(
        {
            "PATH": os.pathsep.join((str(python.parent), str(gh.parent))),
            "HOME": str(root / "home"),
            "USERPROFILE": str(root / "home"),
            "TEMP": str(root / "temp"),
            "TMP": str(root / "temp"),
            "TMPDIR": str(root / "temp"),
            "APPDATA": str(root / "appdata"),
            "LOCALAPPDATA": str(root / "localappdata"),
            "AZURE_CONFIG_DIR": str(root / "azure"),
            "GH_CONFIG_DIR": str(root / "github"),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1",
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_KEYRING_PROVIDER": "disabled",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SITEOPS_REDACT_OUTPUT": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "http_proxy": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "all_proxy": "http://127.0.0.1:9",
            "no_proxy": "",
        }
    )
    return environment


def run(argv: list[str], *, root: Path, log: Path, environment=None, timeout=180) -> bytes:
    process = subprocess.Popen(
        argv,
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    captures = [BoundedCapture.create(8 * 1024 * 1024), BoundedCapture.create(8 * 1024 * 1024)]
    readers = []
    try:
        if process.stdout is None or process.stderr is None:
            raise ArtifactError("Qualification output could not be captured.")
        readers = [
            threading.Thread(target=capture.read, args=(stream,), daemon=True)
            for capture, stream in zip(captures, (process.stdout, process.stderr))
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if any(capture.exceeded.is_set() or capture.failed.is_set() for capture in captures):
                raise ArtifactError(
                    "Qualification process output exceeded its safe capture boundary."
                )
            if time.monotonic() >= deadline:
                raise ArtifactError("Qualification process exceeded its execution deadline.")
            time.sleep(0.02)
        for reader in readers:
            reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers) or any(
            capture.exceeded.is_set() or capture.failed.is_set() for capture in captures
        ):
            raise ArtifactError("Qualification output could not be captured within its limits.")
        if process.returncode:
            raise ArtifactError(
                "The selected engine could not complete isolated installation or qualification."
            )
        return bytes(captures[0].content)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        log.write_bytes(bytes(captures[0].content) + bytes(captures[1].content))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("engine", "workspaces", "trusted-root", "state", "output", "gh"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in (
        "expected-engine-sha",
        "expected-workspace-sha",
        "expected-plan-sha",
        "builder-workflow",
        "platform",
    ):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        if importlib.metadata.version("pip") != "26.2.1":
            raise ArtifactError(
                "Workspace qualification requires the pinned pip 26.2.1 lock reader."
            )
        for name in ("engine", "workspaces", "trusted_root", "state", "output", "gh"):
            setattr(args, name, getattr(args, name).absolute())
        if os.path.lexists(args.output):
            raise ArtifactError("Select a new qualification result file.")
        if args.state.exists() or any(
            args.state.resolve().is_relative_to(path.resolve())
            or path.resolve().is_relative_to(args.state.resolve())
            for path in (args.engine, args.workspaces)
        ):
            raise ArtifactError(
                "Qualification requires new private state outside the selected assets."
            )
        selection = EngineSelection.read(args.engine / SELECTION_NAME, args.expected_engine_sha)
        raw = (args.workspaces / "release-assets.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.expected_workspace_sha:
            raise ArtifactError(
                "Workspace qualification assets differ from the prepared inventory."
            )
        workspace_assets = FrozenReleaseAssets.from_bytes(raw)
        if (
            workspace_assets.source != selection.candidate
            or selection.plan_sha256 != args.expected_plan_sha
        ):
            raise ArtifactError("The selected engine and workspaces describe different candidates.")
        args.state.mkdir(mode=0o700)
        engine_verifier = ReleaseVerifier(
            args.state / "engine-policy",
            args.trusted_root,
            selection.native.source,
            signer=".github/workflows/_siteops-distribution.yaml",
            builder=".github/workflows/release.yaml"
            if selection.reference is not None
            else args.builder_workflow,
        )
        wheel = native_engine_wheel(selection.native.assets)
        for asset in selection.native.assets:
            if hash_file(args.engine / asset.name, limit=asset.size) != (asset.size, asset.sha256):
                raise ArtifactError("An engine input differs from the frozen selection.")
        for asset in (
            next(item for item in selection.native.assets if item.name == ARCHIVE_NAME),
            wheel,
        ):
            proof = next(
                item for item in selection.native.assets if item.name == asset.name + PROOF_SUFFIX
            )
            engine_verifier(
                args.engine / asset.name,
                args.engine / proof.name,
                ArtifactIdentity(asset.name, asset.size, asset.sha256),
            )
        bundle = args.state / "bundle"
        manifest = extract_engine_bundle(args.engine / ARCHIVE_NAME, bundle, selection)
        target = select_target(
            manifest, f"{sys.version_info.major}.{sys.version_info.minor}", args.platform
        )
        if (target.python, target.platform) not in selection.targets:
            raise ArtifactError("The qualification target differs from the selected engine matrix.")
        for asset in workspace_assets.assets:
            if hash_file(args.workspaces / asset.name, limit=asset.size) != (
                asset.size,
                asset.sha256,
            ):
                raise ArtifactError("A workspace input differs from the frozen inventory.")
        workspace_policy = ReleaseVerifier(
            args.state / "workspace-policy",
            args.trusted_root,
            selection.candidate,
            signer=".github/workflows/_workspace-distribution.yaml",
            builder=args.builder_workflow,
        )
        app = args.state / "application"
        python = app / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        environment = application_environment(args.state, python, args.gh)
        run(
            [sys.executable, "-I", "-m", "venv", "--without-pip", str(app)],
            root=args.state,
            log=args.state / "venv.log",
            environment=environment,
        )
        run(
            [
                sys.executable,
                "-I",
                "-m",
                "pip",
                "--python",
                str(python),
                "install",
                "--isolated",
                "--require-hashes",
                "--no-index",
                "--only-binary=:all:",
                "--no-cache-dir",
                "-r",
                str(bundle / "pylock.toml"),
            ],
            root=args.state,
            log=args.state / "install.log",
            environment=environment,
        )
        command = python.parent / ("siteops.exe" if os.name == "nt" else "siteops")
        version = (
            run(
                [str(command), "--version"],
                root=args.state,
                log=args.state / "version.log",
                environment=environment,
                timeout=30,
            )
            .decode()
            .strip()
        )
        if version != "siteops " + selection.version:
            raise ArtifactError("The installed command reports a different engine version.")
        descriptor = next(
            asset for asset in workspace_assets.assets if asset.name == WORKSPACE_RELEASE_NAME
        )
        workspace_descriptor = WorkspaceReleaseAssets.from_bytes(
            (args.workspaces / descriptor.name).read_bytes()
        )
        if workspace_descriptor.revision != selection.candidate["commit"]:
            raise ArtifactError("The workspace descriptor names a different source revision.")
        spec = {
            "engineVersion": selection.version,
            "assets": str(args.workspaces.resolve()),
            "state": str(args.state / "probe-state"),
            "source": {
                **selection.candidate,
                "release": "candidate",
            },
            "descriptor": descriptor.document(),
            "policy": str(workspace_policy.policy_file),
            "trustedRoot": str(workspace_policy.root),
            "workspaceInventorySha256": args.expected_workspace_sha,
        }
        specification = (json.dumps(spec, sort_keys=True) + "\n").encode()
        path = args.state / "probe.json"
        path.write_bytes(specification)
        result = run(
            [
                str(python),
                "-I",
                str(Path(__file__).with_name("probe-installed-workspaces.py")),
                "--spec",
                str(path),
                "--expected-spec-sha",
                hashlib.sha256(specification).hexdigest(),
            ],
            root=args.state,
            log=args.state / "probe.log",
            environment=environment,
            timeout=1800,
        )
        report = json.loads(result)
        if (
            type(report) is not dict
            or report.get("kind") != "WorkspaceEngineQualification"
            or report.get("engineVersion") != selection.version
            or report.get("workspaceInventorySha256") != args.expected_workspace_sha
            or type(report.get("packages")) is not int
            or report["packages"] != len(workspace_descriptor.workspaces)
            or type(report.get("catalogManifests")) is not int or report["catalogManifests"] < 0
            or report.get("platform") != sys.platform
            or report.get("deployment") != "not-run" or report.get("workloadHealth") != "not-checked"
        ):
            raise ArtifactError(
                "The installed engine returned an unsupported qualification result."
            )
        report["engineSelectionSha256"] = args.expected_engine_sha
        report["planSha256"] = args.expected_plan_sha
        report["target"] = {"python": target.python, "platform": target.platform}
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, sort_keys=True) + "\n")
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "Workspace qualification could not complete within its execution boundary."
        )
        print(f"workspace-qualification: {message}", file=sys.stderr)
        return 1
    print("The selected installed engine consumed the frozen workspace packages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
