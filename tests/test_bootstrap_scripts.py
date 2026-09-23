"""Check the two platform entry scripts without contacting package or cloud services."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.shell_helpers import bash_path, required_bash
from tests.verification_helpers import verified_observation

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "bootstrap"
SOURCE_SHA = "c" * 40


def test_private_pipx_venvs_are_created_at_their_retained_paths():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert 'python3 -m venv "$tools/pipx"' in bash
    assert "$tools/pipx-stage" not in bash
    assert "& $python -m venv $installed" in powershell
    assert "Move-Item -LiteralPath $staged" not in powershell


def test_existing_siteops_build_is_rejected_before_any_shared_backend_change():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert bash.index('if [[ -n "$recorded"') < bash.index('"$pipx_bin" upgrade-shared')
    assert powershell.index('if ($recorded -and $recorded -ine') < powershell.index(
        '& $pipx upgrade-shared',
    )
    assert 'if $replace && [[ -n "$recorded" ]]' in bash
    assert 'if ($Replace -and $recorded) { $install += \'--force\' }' in powershell
    assert 'siteops --version' in bash and "siteops.exe" in powershell


def test_windows_native_verification_parses_json_without_powershell_51_jq_quoting():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert "--jq" not in powershell
    assert "ConvertFrom-Json" in powershell
    guide = (ROOT / "docs" / "install-siteops.md").read_text(encoding="utf-8")
    verified = guide.split("### Verify the bootstrap script", 1)[1].split("## Before you start", 1)[0]
    windows = verified.split("Windows PowerShell:", 1)[1]
    assert "--jq" not in windows
    assert "ConvertFrom-Json" in windows


def test_windows_azure_cli_is_selected_independently_of_gh_and_policy_time_is_portable():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    gh_branch = powershell.split("if ($ghVersion -cnotmatch", 1)[1].split("\n}\n", 1)[0]
    assert "if ($WithAzureCli" not in gh_branch
    assert "if ($WithAzureCli -and -not (AzureCli))" in powershell
    assert ".ToString('o')" not in powershell
    assert "yyyy-MM-ddTHH:mm:ss.ffffffzzz" in powershell


@pytest.mark.skipif(sys.platform != "win32", reason="This checks native PowerShell 5.1 argument handling.")
@pytest.mark.parametrize("accepted", [False, True])
def test_windows_bootstrap_checks_native_verifier_certificate_before_bundle_use(
    tmp_path, accepted,
):
    script = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    block = script.split('    $signer = "', 1)[1].split("    $bundleId =", 1)[0]
    block = '    $signer = "' + block
    observation = verified_observation(
        "Azure/digital-ops-scale-kit", SOURCE_SHA, "refs/heads/main",
        ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
    )
    if not accepted:
        observation["verificationResult"]["signature"]["certificate"]["buildConfigURI"] = "PRIVATE_WRONG"
    evidence = tmp_path / "observation.json"
    evidence.write_text(json.dumps([observation]), encoding="utf-8")
    wrapper = tmp_path / "verify.ps1"
    wrapper.write_text(
        """function gh.exe { Get-Content -LiteralPath $env:TEST_EVIDENCE -Raw; $global:LASTEXITCODE = 0 }
function Stage([string]$message) { }
function Fail([string]$message) { throw $message }
$ErrorActionPreference = 'Stop'
$gh = 'gh.exe'
$archive = 'unopened.zip'
$Repository = 'Azure/digital-ops-scale-kit'
$SourceRef = 'refs/heads/main'
$Caller = 'release.yaml'
$SourceCommit = '""" + SOURCE_SHA + "'\n" + block + "\n'CERTIFICATE_ACCEPTED'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        env={**os.environ, "TEST_EVIDENCE": str(evidence)},
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert ("CERTIFICATE_ACCEPTED" in result.stdout) is accepted
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 5.1 is available on Windows.")
def test_windows_retained_release_key_binds_exact_selection(tmp_path):
    script = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    block = script.split('    $identity = (', 1)[1].split('    $cache = Join-Path', 1)[0]
    block = '    $identity = (' + block
    statement = (
        "$Repository='Azure/digital-ops-scale-kit';$Release='siteops/v1.0.0b1';"
        "$SourceCommit='" + SOURCE_SHA + "';$SourceRef='refs/heads/main';"
        "$Caller='release.yaml';$data='unused';"
        + block + "\n$cacheId\n"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", statement],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    selected = (
        "Azure/digital-ops-scale-kit", "siteops/v1.0.0b1",
        SOURCE_SHA, "refs/heads/main", "release.yaml",
    )
    expected = hashlib.sha256(("\0".join(selected) + "\0").encode()).hexdigest()
    assert result.stdout.strip() == expected


def test_bash_is_portable_lf_and_parses():
    script = SCRIPTS / "siteops-bootstrap.sh"
    content = script.read_bytes()
    assert content.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r" not in content
    result = subprocess.run(
        [str(required_bash()), "-n", bash_path(script)],
        capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_retained_bundle_accepts_matching_files_but_rejects_tampering(tmp_path):
    script = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    found = re.search(
        r'python3 - "\$bundle" <<\'PY\' \|\|\n'
        r'    fail "The retained bundle contents differ from the authenticated archive\."\n'
        r"(.*?)\nPY", script, flags=re.DOTALL,
    )
    assert found, "The retained bundle verifier is missing."
    bundle = tmp_path / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    files = {"pylock.toml": b"locked bytes\n", "wheels/fixture.whl": b"wheel bytes"}
    entries = []
    for name, data in files.items():
        destination = bundle / name
        destination.write_bytes(data)
        entries.append({
            "path": name, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    (bundle / "bundle.json").write_text(json.dumps({"files": entries}), encoding="utf-8")

    def verify():
        return subprocess.run(
            [sys.executable, "-c", found.group(1), str(bundle)],
            cwd=tmp_path, capture_output=True, text=True, timeout=20,
        )

    assert verify().returncode == 0
    (bundle / "pylock.toml").write_bytes(b"altered bytes\n")
    assert verify().returncode != 0
    (bundle / "pylock.toml").write_bytes(files["pylock.toml"])
    (bundle / "unexpected").write_bytes(b"operator file")
    assert verify().returncode != 0
    (bundle / "unexpected").unlink()
    assert verify().returncode == 0


def test_both_scripts_keep_the_azure_and_source_boundaries_explicit():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    for script in (bash, powershell):
        assert "gh auth login" not in script and "az login" not in script
        assert "siteops deploy" not in script
        assert "attestation verify" in script
        assert "siteops-install.zip.attestation.jsonl" in script
        assert "--cert-identity" in script and "--source-digest" in script
        assert "--tlsv1.2" in script
        assert "buildConfigURI" in script and "runnerEnvironment" in script
        assert script.index("attestation verify") < script.index("--lock")


@pytest.mark.skipif(sys.platform != "linux", reason="Ubuntu preview runs on the Linux runner.")
def test_bash_preview_and_unattended_refusal_do_not_acquire_tools(tmp_path):
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    if "ID=ubuntu" not in os_release or 'VERSION_ID="24.04"' not in os_release:
        pytest.skip("Requires Ubuntu 24.04.")
    script = SCRIPTS / "siteops-bootstrap.sh"
    arguments = ["bash", str(script), "--release", "siteops/v1.0.0b1", "--source-commit", SOURCE_SHA]
    env = {**os.environ, "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "state")}
    preview = subprocess.run(
        [*arguments, "--dry-run"], input="", text=True, capture_output=True, env=env, timeout=20,
    )
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "No tools or content were downloaded" in preview.stdout
    denied = subprocess.run(
        arguments, input="", text=True, capture_output=True, env=env, timeout=20,
    )
    assert denied.returncode != 0
    assert "pass --yes" in denied.stderr
    assert not (tmp_path / "state").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="The Ubuntu runner exercises the script journey.")
def test_ubuntu_bootstrap_recovers_and_requires_explicit_replacement(tmp_path):
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    if "ID=ubuntu" not in os_release or 'VERSION_ID="24.04"' not in os_release:
        pytest.skip("Requires Ubuntu 24.04.")
    harness = ROOT / "tests" / "fixtures" / "bootstrap-harness.sh"
    script = SCRIPTS / "siteops-bootstrap.sh"
    result = subprocess.run(
        [str(required_bash()), bash_path(harness), bash_path(script)],
        cwd=tmp_path, env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C",
        }, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no network" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell preview runs on Windows.")
def test_powershell_preview_and_invalid_identity_do_not_acquire_tools(tmp_path):
    script = SCRIPTS / "siteops-bootstrap.ps1"
    path = shutil.which("pwsh") or shutil.which("powershell")
    if path is None:
        pytest.skip("PowerShell is unavailable.")
    arguments = [
        path, "-NoProfile", "-File", str(script),
        "-Release", "siteops/v1.0.0b1", "-SourceCommit", SOURCE_SHA,
    ]
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path / "state")}
    preview = subprocess.run(
        [*arguments, "-DryRun"], input="", text=True, capture_output=True, env=env, timeout=20,
    )
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "No tools or content were downloaded" in preview.stdout
    denied = subprocess.run(
        [*arguments[:-1], "bad", "-Yes"], input="", text=True,
        capture_output=True, env=env, timeout=20,
    )
    assert denied.returncode != 0
    assert "Select an exact release" in denied.stderr
    assert not (tmp_path / "state").exists()
