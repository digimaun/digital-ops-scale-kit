"""Run the qualification probe through an installed real wheel, outside the checkout."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from siteops import __version__
from siteops.workspace_source import ArtifactIdentity, WorkspaceReleaseAssets, WorkspaceReleaseEntry
from tests.installed_runtime import build_engine_wheel, install_engine

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe-installed-workspaces.py"


@pytest.fixture(scope="module")
def application(tmp_path_factory):
    root = tmp_path_factory.mktemp("qualified-engine")
    wheel = build_engine_wheel(root)
    app = install_engine(root / "runtime", wheel)
    result = subprocess.run(
        [str(app.python), "-I", str(ROOT / "tests" / "fixtures" / "prepare_installed_project.py"), str(app.root)],
        cwd=app.root / "unrelated", env=app.environment, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    identities = json.loads(result.stdout)
    assets = app.root / "verified-assets"
    assets.mkdir()
    for name in ("workspace.zip", "proof.jsonl"):
        shutil.copyfile(app.root / name, assets / name)
    entry = WorkspaceReleaseEntry(
        "workspace", "example.storage", "7",
        ArtifactIdentity("workspace.zip", (assets / "workspace.zip").stat().st_size, identities["package"]),
        ArtifactIdentity("proof.jsonl", (assets / "proof.jsonl").stat().st_size, identities["proof"]),
    )
    descriptor = WorkspaceReleaseAssets("a" * 40, (entry,)).serialized()
    (assets / "siteops-workspaces.json").write_bytes(descriptor)
    (app.root / "unrelated" / "siteops.py").write_text("raise AssertionError('Imported cwd engine')\n")
    return app, assets, descriptor


def invoke(application, name, *, version=__version__, isolated=True, wrong_spec=False):
    app, assets, descriptor = application
    spec = {
        "engineVersion": version, "assets": str(assets), "state": str(app.root / name),
        "source": {"repository": "example/content", "commit": "a" * 40, "ref": "refs/heads/main", "release": "release-7"},
        "descriptor": {"name": "siteops-workspaces.json", "size": len(descriptor), "sha256": hashlib.sha256(descriptor).hexdigest()},
        "policy": str(app.root / "policy.json"), "trustedRoot": str(app.root / "trusted-root.json"),
        "workspaceInventorySha256": "c" * 64,
    }
    raw = json.dumps(spec).encode()
    path = app.root / (name + ".json")
    path.write_bytes(raw)
    args = [str(app.python), *(["-I"] if isolated else []), str(PROBE),
            "--spec", str(path), "--expected-spec-sha", "d" * 64 if wrong_spec else hashlib.sha256(raw).hexdigest()]
    return subprocess.run(
        args, cwd=app.root / "unrelated",
        env={**app.environment, "PYTHONPATH": str(ROOT)}, capture_output=True,
        text=True, timeout=120,
    )


def test_probe_consumes_frozen_content_with_the_actual_installed_engine(application):
    result = invoke(application, "qualified")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["engineVersion"] == __version__
    assert report["packages"] == 1 and report["catalogManifests"] == 1
    assert report["workspaceInventorySha256"] == "c" * 64
    assert report["deployment"] == "not-run" and report["workloadHealth"] == "not-checked"
    assert "guarded-catalog-load" in report["checks"]
    app, _, _ = application
    assert list((app.root / "qualified" / "operator" / "sites").iterdir()) == []


@pytest.mark.parametrize("fault", ["version", "isolation", "spec"])
def test_probe_rejects_wrong_version_or_checkout_fallback(application, fault):
    result = invoke(
        application, "rejected-" + fault,
        version="99.0.0" if fault == "version" else __version__,
        isolated=fault != "isolation", wrong_spec=fault == "spec",
    )
    assert result.returncode != 0
    assert not result.stdout
    assert not (application[0].root / ("rejected-" + fault)).exists()
