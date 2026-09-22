"""Exercise guided target selection through the public command path."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from siteops.cli import main
from siteops.compilation import TemplateCompilationSession
from siteops.guided_inputs import contract_path, load_contract
from siteops.models import Site
from siteops.orchestrator import Orchestrator
from siteops.package_builder import _source_files
from siteops.planning import (
    DeploymentOperation,
    LiteralValue,
    PlanDisposition,
    PlanIntent,
    PlanStatus,
    resolve_plan_value,
)


@pytest.fixture
def guided_workspace(complete_workspace):
    contract = {
        "apiVersion": "siteops.inputs/v1",
        "kind": "SiteInputContract",
        "inputs": [
            {
                "name": "siteName",
                "type": "string",
                "description": "Site identity.",
                "sitePath": "name",
            },
            {
                "name": "subscription",
                "type": "string",
                "description": "Subscription identity.",
                "sitePath": "subscription",
            },
            {
                "name": "location",
                "type": "string",
                "description": "Deployment region.",
                "sitePath": "location",
            },
        ],
    }
    path = contract_path(complete_workspace / "manifests" / "test-manifest.yaml")
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return complete_workspace


def _invoke(argv):
    with patch.object(sys, "argv", ["siteops", *argv]):
        with pytest.raises(SystemExit) as stopped:
            main()
    return stopped.value.code


def _manifest(workspace: Path) -> str:
    return str(workspace / "manifests" / "test-manifest.yaml")


def _input_file(path: Path, *, name: str = "one") -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops.inputs/v1",
                "kind": "SiteInputValues",
                "values": {
                    "siteName": name,
                    "subscription": "00000000-0000-0000-0000-000000000001",
                    "location": "eastus",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_inputs_inspection_and_example_are_not_executable(
    guided_workspace, tmp_path, capsys,
):
    example = tmp_path / "answers.yaml"
    args = [
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--example", str(example), "--output", "json",
    ]
    assert _invoke(args) == 0
    document = json.loads(capsys.readouterr().out)
    assert {item["name"] for item in document["inputs"]} == {
        "siteName", "subscription", "location",
    }
    assert yaml.safe_load(example.read_text(encoding="utf-8"))["values"] == {
        "siteName": None,
        "subscription": None,
        "location": None,
    }
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input-file", str(example),
    ]) == 1
    assert "required" in capsys.readouterr().out.lower()
    assert _invoke(args) == 1
    assert "exists" in capsys.readouterr().err.lower()


def test_plain_input_inspection_escapes_authored_terminal_controls(
    guided_workspace, capsys,
):
    path = contract_path(guided_workspace / "manifests" / "test-manifest.yaml")
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    contract["inputs"][0]["description"] = "A name.\x1b[31m"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")

    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
    ]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert r"\u001b" in output


def test_input_file_and_inline_override_manifest_fleet_target(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--input-file", str(answers), "--input", "siteName=two",
        "--describe", "--output", "json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in result["plan"]["targets"]] == ["two"]
    assert result["plan"]["manifest"]["targetSelection"] == "explicit-site"


def test_inline_answers_can_complete_generated_example_file(
    guided_workspace, tmp_path, capsys,
):
    example = tmp_path / "example.yaml"
    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--example", str(example),
    ]) == 0
    capsys.readouterr()

    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input-file", str(example),
        "--input", "siteName=one",
        "--input", "subscription=00000000-0000-0000-0000-000000000001",
        "--input", "location=eastus",
        "--output", "json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in result["plan"]["targets"]] == ["one"]


def test_deploy_passes_one_explicit_site_to_existing_executor_path(
    guided_workspace, tmp_path,
):
    answers = _input_file(tmp_path / "answers.yaml")
    with (
        patch.object(
            Orchestrator, "deploy", return_value=SimpleNamespace(exit_code=0),
        ) as deploy,
        patch("siteops.cli._write_run_result"),
    ):
        assert _invoke([
            "-w", str(guided_workspace), "deploy", _manifest(guided_workspace),
            "--input-file", str(answers),
        ]) == 0

    deploy.assert_called_once()
    assert deploy.call_args.kwargs["selector"] is None
    sites = deploy.call_args.kwargs["sites"]
    assert len(sites) == 1
    assert isinstance(sites[0], Site)
    assert sites[0].name == "one"


def test_complete_standalone_site_replaces_manifest_target(
    guided_workspace, tmp_path, capsys,
):
    standalone = tmp_path / "standalone.yaml"
    standalone.write_text(
        yaml.safe_dump({
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "name": "explicit",
            "subscription": "00000000-0000-0000-0000-000000000001",
            "location": "eastus",
        }),
        encoding="utf-8",
    )
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--site-file", str(standalone), "--output", "json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in result["plan"]["targets"]] == ["explicit"]


def test_validate_checks_explicit_site_without_configured_selection(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    assert _invoke([
        "-w", str(guided_workspace), "validate", _manifest(guided_workspace),
        "--input-file", str(answers),
    ]) == 0
    assert "Manifest is valid" in capsys.readouterr().out


def test_mixed_explicit_and_fleet_target_fails_before_execution(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    assert _invoke([
        "-w", str(guided_workspace), "deploy", _manifest(guided_workspace),
        "--input-file", str(answers), "-l", "name=test-site",
        "--output", "json",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] != "succeeded"
    assert output["diagnostics"][0]["code"] == "plan.targeting.conflict"
    assert "selector" in output["diagnostics"][0]["summary"].lower()


def test_missing_inputs_have_actionable_machine_diagnostic(
    guided_workspace, capsys,
):
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input", "siteName=one", "--output", "json",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "inputs.invalid"
    assert "siteops inputs" in output["diagnostics"][0]["summary"]


def test_ci_missing_input_reports_safe_field_name(
    guided_workspace, monkeypatch, capsys,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input", "siteName=one",
        "--output", "json",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "inputs.invalid"
    assert "subscription" in output["diagnostics"][0]["summary"]


def test_ci_deploy_missing_input_reports_safe_field_name(
    guided_workspace, monkeypatch, capsys,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    assert _invoke([
        "-w", str(guided_workspace), "deploy", _manifest(guided_workspace),
        "--input", "siteName=one", "--output", "json",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "inputs.invalid"
    assert "subscription" in output["diagnostics"][0]["summary"]


def test_saved_site_is_normal_config_and_not_overwritten(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    site_file = guided_workspace / "sites" / "one.yaml"
    args = [
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--save-site", str(site_file),
    ]
    assert _invoke(args) == 0
    capsys.readouterr()
    site = Site.from_file(site_file)
    assert site.name == "one"
    assert site.location == "eastus"
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "-l", "name=one", "--describe",
    ]) == 0
    capsys.readouterr()
    assert _invoke(args) == 1
    assert "exists" in capsys.readouterr().err.lower()


def test_generated_answers_cannot_modify_the_content_cache(
    guided_workspace, tmp_path, monkeypatch, capsys,
):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(cache))
    destination = cache / "answers.yaml"

    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--example", str(destination),
    ]) == 1
    assert not destination.exists()
    assert "cache" in capsys.readouterr().err.lower()


def test_explicit_target_cannot_use_cached_package_inputs(
    guided_workspace, tmp_path, monkeypatch, capsys,
):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(cache))
    answers = _input_file(cache / "answers.yaml")

    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input-file", str(answers),
    ]) == 1
    assert "cache" in capsys.readouterr().out.lower()


def test_protected_contract_cannot_emit_a_site(
    guided_workspace, tmp_path, capsys,
):
    input_contract = contract_path(guided_workspace / "manifests" / "test-manifest.yaml")
    contract = yaml.safe_load(input_contract.read_text(encoding="utf-8"))
    contract["inputs"].append({
        "name": "password",
        "type": "string",
        "description": "Protected value.",
        "sitePath": "properties.password",
        "required": False,
        "sensitive": True,
    })
    input_contract.write_text(yaml.safe_dump(contract), encoding="utf-8")
    answers = _input_file(tmp_path / "answers.yaml")
    site_file = tmp_path / "site.yaml"

    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--save-site", str(site_file),
    ]) == 1
    assert "protected" in capsys.readouterr().err.lower()
    assert not site_file.exists()


def test_manifest_without_contract_accepts_complete_site_only(
    complete_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    assert _invoke([
        "-w", str(complete_workspace), "plan", _manifest(complete_workspace),
        "--describe", "--input-file", str(answers),
    ]) == 1
    assert "no typed input contract" in capsys.readouterr().out.lower()
    site_file = tmp_path / "existing.yaml"
    site_file.write_text(
        yaml.safe_dump({
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "name": "standalone",
            "subscription": "00000000-0000-0000-0000-000000000001",
            "location": "eastus",
        }),
        encoding="utf-8",
    )
    assert _invoke([
        "-w", str(complete_workspace), "plan", _manifest(complete_workspace),
        "--describe", "--site-file", str(site_file), "--output", "json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in result["plan"]["targets"]] == [
        "standalone"
    ]


def test_aio_contract_is_included_in_workspace_package_source():
    root = Path(__file__).resolve().parents[1]
    authored = (
        "workspaces/iot-operations/manifests/aio-install/inputs.yaml"
    )
    assert authored in _source_files(root, "workspaces/iot-operations", ())


def test_aio_input_inspection_distinguishes_required_and_defaulted(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    assert _invoke([
        "-w", str(workspace), "inputs", "aio-install", "--output", "json",
    ]) == 0
    document = json.loads(capsys.readouterr().out)
    fields = {field["name"]: field for field in document["inputs"]}
    assert fields["clusterName"]["status"] == "required"
    assert fields["environment"]["status"] == "required"
    assert fields["country"]["status"] == "required"
    assert fields["aioRelease"]["status"] == "defaulted"
    assert fields["aioRelease"]["default"] == "2608"
    assert fields["enableCertManager"]["default"] is True


def test_aio_boolean_defaults_use_answer_file_spelling(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    assert _invoke(["-w", str(workspace), "inputs", "aio-install"]) == 0
    output = capsys.readouterr().out
    assert "Default: true" in output
    assert "Default: True" not in output


def test_aio_inputs_prepare_one_explicit_target_without_example_site(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    assert _invoke([
        "-w", str(workspace), "plan", "aio-install", "--describe",
        "--input", "siteName=plant-one",
        "--input", "subscription=00000000-0000-0000-0000-000000000001",
        "--input", "resourceGroup=rg-existing",
        "--input", "location=eastus",
        "--input", "clusterName=existing-arc",
        "--input", "environment=dev",
        "--input", "country=US",
        "--output", "json",
    ]) == 0
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert [target["name"] for target in document["plan"]["targets"]] == [
        "plant-one"
    ]
    assert document["plan"]["manifest"]["manifestSelector"] is None
    assert document["plan"]["manifest"]["targetSelection"] == "explicit-site"
    assert "replaces manifest targeting" in output.err
    dispositions = {
        operation["identity"]["step"]: operation["disposition"]
        for operation in document["plan"]["targets"][0]["operations"]
    }
    assert dispositions["aio-instance"] == "execute"
    assert dispositions["resolve-aio"] == "skip"
    assert dispositions["secretsync"] == "skip"


def test_aio_executable_preparation_resolves_country_and_environment_tags(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    manifest = workspace / "manifests" / "aio-install" / "manifest.yaml"
    contract = load_contract(manifest)
    site = contract.resolve(inline=[
        "siteName=plant-one",
        "subscription=00000000-0000-0000-0000-000000000001",
        "resourceGroup=rg-existing",
        "location=eastus",
        "clusterName=existing-arc",
        "environment=dev",
        "country=US",
    ])

    def runner(argv: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ("version", "--output", "json"):
            return subprocess.CompletedProcess(argv, 0, '{"azure-cli":"test"}', "")
        if argv[1:] == ("bicep", "version"):
            return subprocess.CompletedProcess(argv, 0, "Bicep CLI version test", "")
        if argv[1:3] != ("bicep", "build"):
            raise AssertionError(f"Unexpected local tool invocation: {argv}")
        source = Path(argv[argv.index("--file") + 1])
        target = Path(argv[argv.index("--outfile") + 1])
        template = {
            "$schema": (
                "https://schema.management.azure.com/schemas/"
                "2019-04-01/deploymentTemplate.json#"
            ),
            "contentVersion": "1.0.0.0",
            "parameters": {
                "tags": {"type": "object"},
            } if source.name in {"schema-registry.bicep", "adr-ns.bicep"} else {},
            "resources": [],
            "outputs": {
                "schemaRegistry": {"type": "object"},
                "adrNamespace": {"type": "object"},
                "clExtensionIds": {"type": "array"},
            },
        }
        target.write_text(json.dumps(template), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    session = TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=lambda name: str(tmp_path / "tools" / name),
    )
    with patch("siteops.orchestrator.TemplateCompilationSession", return_value=session):
        result = Orchestrator(workspace).build_plan(
            manifest, sites=[site], intent=PlanIntent.EXECUTABLE,
        )
        unlabelled = Orchestrator(workspace).build_plan(
            manifest, sites=[replace(site, labels={})], intent=PlanIntent.EXECUTABLE,
        )
    assert result.status is PlanStatus.PLANNED, result.diagnostics
    assert unlabelled.status is PlanStatus.INVALID
    assert any(
        diagnostic.code == "operation-preparation.invalid"
        for diagnostic in unlabelled.diagnostics
    )
    assert result.plan is not None
    operations = {
        operation.identity.step: operation
        for operation in result.plan.targets[0].operations
    }
    for step in ("schema-registry", "adr-ns"):
        operation = operations[step]
        assert operation.disposition is PlanDisposition.EXECUTE
        assert isinstance(operation.details, DeploymentOperation)
        assert operation.details.parameters is not None
        tags = next(
            entry.value for entry in operation.details.parameters.entries
            if isinstance(entry.key, LiteralValue) and entry.key.value == "tags"
        )
        assert resolve_plan_value(tags, {})["environment"] == "dev"
        assert resolve_plan_value(tags, {})["country"] == "US"
