"""Exercise the native installation guide's command and path contracts."""

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from tests.shell_helpers import bash_path, run_script, write_executable
from tests.verification_helpers import verified_observation

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


GUIDE = Path(__file__).resolve().parent.parent / "docs" / "install-siteops.md"


def _section(heading: str) -> str:
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split(heading + "\n", 1)[1]
    return re.split(r"\n#{1,3} ", section, maxsplit=1)[0]


def _block(section: str, language: str) -> str:
    return re.search(rf"```{language}\n(.*?)\n```", section, re.DOTALL).group(1)


@pytest.mark.parametrize("heading", [
    "### Bootstrap from HTTPS", "### Verify the bootstrap script",
])
def test_bootstrap_bash_examples_parse_without_executing_external_tools(tmp_path, heading):
    section = _section(heading)
    body = _block(section, "bash")
    script = tmp_path / "example.sh"
    script.write_text(body, encoding="utf-8", newline="\n")
    bash = shutil.which("bash")
    if sys.platform == "win32":
        from tests.shell_helpers import required_bash

        bash = str(required_bash())
    result = subprocess.run(
        [bash, "-n", bash_path(script)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--source-commit" in body and "--release" in body
    assert "--tlsv1.2" in body


@pytest.mark.parametrize("heading", [
    "### Bootstrap from HTTPS", "### Verify the bootstrap script",
])
def test_bootstrap_windows_examples_parse(tmp_path, heading):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable.")
    body = _block(_section(heading), "powershell")
    example = tmp_path / "example.ps1"
    example.write_text(body, encoding="utf-8")
    parser = (
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile($env:TEST_SCRIPT,"
        "[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count){$errors|ForEach-Object{Write-Error $_};exit 1}"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", parser],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "TEST_SCRIPT": str(example)},
    )
    assert result.returncode == 0, result.stderr
    assert "--tlsv1.2" in body


@pytest.mark.parametrize("verified", [False, True])
def test_verified_bootstrap_guide_runs_script_only_after_matching_proof(tmp_path, verified):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "curl", """#!/usr/bin/env bash
while (($#)); do
  if [[ "$1" == "--output" ]]; then output="$2"; shift 2; else shift; fi
done
printf 'echo SCRIPT_RAN\\n' > "$output"
""")
    write_executable(bin_dir / "gh", """#!/usr/bin/env bash
[[ "$1 $2" == "attestation verify" ]] || exit 99
printf '%s\\n' "$@" > "$TEST_GH_ARGUMENTS"
printf '%s\\n' "$TEST_VERIFIED"
""")
    log = tmp_path / "gh-arguments.txt"
    result = run_script(
        _block(_section("### Verify the bootstrap script"), "bash"),
        tmp_path, {
            "TEST_GH_ARGUMENTS": bash_path(log),
            "TEST_VERIFIED": "true" if verified else "false",
        },
    )
    assert (result.returncode == 0) is verified
    assert ("SCRIPT_RAN" in result.stdout) is verified
    assert "--cert-identity" in log.read_text(encoding="utf-8")
    assert "--source-digest" in log.read_text(encoding="utf-8")
    assert "--signer-digest" in log.read_text(encoding="utf-8")
    assert "--bundle" in log.read_text(encoding="utf-8")


def test_online_transition_selects_a_release_wheel_instead_of_an_index_package():
    section = _section("## Select another build, repair, or remove")
    manifest = tomllib.loads(_block(section, "toml"))
    [declaration] = manifest["dependency-groups"]["siteops"]
    requirement = Requirement(declaration)
    assert requirement.name == "siteops"
    assert requirement.url.startswith(
        "https://github.com/Azure/digital-ops-scale-kit/releases/download/",
    )
    assert requirement.url.endswith("/siteops-<version>-py3-none-any.whl")
    assert not requirement.specifier
    assert "lock" not in manifest["tool"]["pipx"]["tools"]["siteops"]
    assert "PIP_ONLY_BINARY=:all:" in section
    assert "PIPX_FETCH_PYTHON=never" in section


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 5.1 is available on Windows.")
@pytest.mark.parametrize("verified", [False, True])
def test_verified_bootstrap_windows_requires_the_certificate_before_execution(tmp_path, verified):
    section = _section("### Verify the bootstrap script")
    body = _block(section, "powershell").replace(
        "<approved-release-tag>", "siteops/v1.0.0b1",
    ).replace("<full-source-commit>", "c" * 40)
    observation = verified_observation(
        "Azure/digital-ops-scale-kit", "c" * 40, "refs/heads/main",
        ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
    )
    if not verified:
        observation["verificationResult"]["signature"]["certificate"]["runnerEnvironment"] = "PRIVATE_WRONG"
    evidence = tmp_path / "observation.json"
    evidence.write_text(json.dumps([observation]), encoding="utf-8")
    script = tmp_path / "verified-example.ps1"
    script.write_text(
        """function icacls { $global:LASTEXITCODE = 0 }
function curl.exe {
    $target = $args[[array]::IndexOf($args, '--output') + 1]
    Set-Content -LiteralPath $target -Value 'test bytes'
    $global:LASTEXITCODE = 0
}
function gh.exe {
    Get-Content -LiteralPath $env:TEST_EVIDENCE -Raw
    $global:LASTEXITCODE = 0
}
function powershell.exe { 'SCRIPT_RAN'; $global:LASTEXITCODE = 0 }
""" + body, encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        env={**os.environ, "TEMP": str(tmp_path), "TEST_EVIDENCE": str(evidence)},
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert (result.returncode == 0) is verified, result.stdout + result.stderr
    assert ("SCRIPT_RAN" in result.stdout) is verified
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("state", [
    "verified", "rejected", "existing", "subjectAlternativeName", "issuer",
    "sourceRepositoryURI", "sourceRepositoryDigest", "sourceRepositoryRef",
    "buildSignerDigest", "buildConfigURI", "buildConfigDigest", "runnerEnvironment",
    "runner-case", "runner-type", "media-type",
])
def test_guide_authenticates_before_creating_retained_files(tmp_path, shell, state):
    if shell == "powershell" and (sys.platform != "win32" or not shutil.which("pwsh")):
        pytest.skip("The PowerShell retention example uses Windows identity and ACL tools.")
    download = tmp_path / "download with spaces"
    download.mkdir()
    archive = download / "siteops-install.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("payload.txt", "authenticated test payload")
    (download / "siteops-install.zip.attestation.jsonl").write_bytes(b"opaque fixture proof")
    identity = hashlib.sha256(archive.read_bytes()).hexdigest()
    destination = tmp_path / "data" / "siteops" / "bundles" / identity
    if state == "existing":
        destination.mkdir(parents=True)
        (destination / "operator.txt").write_text("preserve")
    body = _block(_section("### Authenticate and retain the bundle"), shell)
    body = body.replace(
        "<download directory>", str(download) if shell == "powershell" else bash_path(download),
    ).replace("<full source commit from the selected official release>", "a" * 40)
    code = 9 if state == "rejected" else 0
    observation = verified_observation(
        "Azure/digital-ops-scale-kit", "a" * 40, "refs/heads/main",
        ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
    )
    observations = [observation]
    if state not in {"verified", "rejected", "existing"}:
        changed = copy.deepcopy(observation)
        certificate = changed["verificationResult"]["signature"]["certificate"]
        if state == "runner-case":
            certificate["runnerEnvironment"] = "SELF-HOSTED"
        elif state == "runner-type":
            certificate["runnerEnvironment"] = ["self-hosted"]
        elif state == "media-type":
            changed["verificationResult"]["mediaType"] = [changed["verificationResult"]["mediaType"]]
        else:
            certificate[state] = "PRIVATE_WRONG"
        observations.append(changed)
    evidence = tmp_path / "observations.json"
    evidence.write_text(json.dumps(observations), encoding="utf-8")
    arguments = tmp_path / "arguments.json"
    if shell == "powershell":
        script = tmp_path / "retain.ps1"
        script.write_text(
            f"""function gh {{
    ConvertTo-Json -InputObject @($args) -Compress | Set-Content -LiteralPath $env:TEST_ARGUMENTS
    Get-Content -LiteralPath $env:TEST_EVIDENCE -Raw
    $global:LASTEXITCODE = {code}
}}
""" + body, encoding="utf-8",
        )
        result = subprocess.run(
            [shutil.which("pwsh"), "-NoProfile", "-File", str(script)], cwd=tmp_path,
            env={
                **os.environ, "LOCALAPPDATA": str(tmp_path / "data"),
                "TEST_ARGUMENTS": str(arguments), "TEST_EVIDENCE": str(evidence),
            },
            capture_output=True, text=True, timeout=30,
        )
    else:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        write_executable(bin_dir / "gh", f"""#!/usr/bin/env bash
[[ "$1 $2" == "attestation verify" ]] || exit 99
printf '%s\\n' "$@" > "$TEST_ARGUMENTS"
cat "$TEST_EVIDENCE"
exit {code}
""")
        result = run_script(
            'python3() { "$TEST_PYTHON" "$@"; }\n' + body,
            tmp_path, {
                "TEST_PYTHON": Path(sys.executable).as_posix(),
                "TEST_ARGUMENTS": bash_path(arguments), "TEST_EVIDENCE": bash_path(evidence),
                "XDG_DATA_HOME": bash_path(tmp_path / "data"),
            },
        )
    argv = (
        json.loads(arguments.read_text(encoding="utf-8-sig"))
        if shell == "powershell" else arguments.read_text().splitlines()
    )
    expected = {
        "--repo": "Azure/digital-ops-scale-kit",
        "--cert-identity": "https://github.com/Azure/digital-ops-scale-kit/.github/workflows/_siteops-distribution.yaml@refs/heads/main",
        "--source-ref": "refs/heads/main", "--source-digest": "a" * 40, "--signer-digest": "a" * 40,
        "--cert-oidc-issuer": "https://token.actions.githubusercontent.com",
        "--predicate-type": "https://slsa.dev/provenance/v1", "--hostname": "github.com",
        "--digest-alg": "sha256", "--format": "json",
    }
    assert argv[:2] == ["attestation", "verify"]
    assert argv[2] == (str(archive) if shell == "powershell" else bash_path(archive))
    assert argv[argv.index("--bundle") + 1] == argv[2] + ".attestation.jsonl"
    assert "--deny-self-hosted-runners" not in argv
    for flag, value in expected.items():
        assert argv.count(flag) == 1 and argv[argv.index(flag) + 1] == value
    if state == "verified":
        assert result.returncode == 0, result.stdout + result.stderr
        assert (destination / "payload.txt").read_text() == "authenticated test payload"
    else:
        assert result.returncode != 0
        assert not (destination / "payload.txt").exists()
        if state != "existing":
            assert not destination.exists()
        else:
            assert (destination / "operator.txt").read_text() == "preserve"
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr


@pytest.mark.parametrize("language", ["powershell", "bash"])
def test_locked_install_explicitly_sets_the_path_returned_from_scoped_extraction(language):
    section = _section("### Install from the verified lock")
    command = _block(section, language)
    variable = "$bundle" if language == "powershell" else "bundle"
    assert command.startswith(variable + (" = " if language == "powershell" else "="))
    assert "<retained bundle directory printed by the extraction step>" in command
    assert 'pipx install siteops --lock "$bundle' in command


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\owned tools\wheels", "file:///C:/owned%20tools/wheels"),
        ("/tmp/owned tools/wheels", "file:///tmp/owned%20tools/wheels"),
    ],
)
def test_powershell_wheelhouse_uri_handles_windows_and_unix_paths(tmp_path, path, expected):
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("Executing the PowerShell guide requires pwsh.")
    body = _block(_section("### Provision the pipx backend that reads the lock"), "powershell")
    assignment = next(line for line in body.splitlines() if line.strip().startswith("$wheelhouse ="))
    script = tmp_path / "wheelhouse.ps1"
    script.write_text(
        "param([string]$tools)\n" + assignment + "\n$wheelhouse\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(script), path],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("download_exit", [0, 23])
def test_powershell_backend_recipe_preserves_space_paths_and_stops_on_failure(
    tmp_path, download_exit,
):
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("Executing the PowerShell guide requires pwsh.")
    temporary = tmp_path / "temporary files with spaces"
    temporary.mkdir()
    capture = tmp_path / "pipx.json"
    body = _block(_section("### Provision the pipx backend that reads the lock"), "powershell")
    script = tmp_path / "backend.ps1"
    script.write_text(
        f"""
function python {{ $global:LASTEXITCODE = {download_exit} }}
function pipx {{
    ConvertTo-Json -InputObject @($args) -Compress |
        Set-Content -LiteralPath $env:SITEOPS_GUIDE_CAPTURE -Encoding utf8
    $global:LASTEXITCODE = 0
}}
""" + body,
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(script)],
        cwd=tmp_path,
        env={**os.environ, "TEMP": str(temporary), "SITEOPS_GUIDE_CAPTURE": str(capture)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    if download_exit:
        assert result.returncode != 0
        assert "backend wheel could not be downloaded" in result.stderr
        assert not capture.exists()
    else:
        assert result.returncode == 0, result.stderr
        arguments = json.loads(capture.read_text(encoding="utf-8-sig"))
        assert arguments[:2] == ["upgrade-shared", "--pip-args"]
        assert "--force-reinstall" in arguments[2]
        uri = arguments[2].split("--find-links=", 1)[1]
        assert uri.startswith("file:///")
        assert "%20" in uri and " " not in uri
