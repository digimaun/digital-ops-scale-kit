# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from siteops.reporting import _KIND as DEPLOYMENT_KIND

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "e2e-test.yaml"
ACTION = ROOT / ".github" / "actions" / "setup-published-siteops" / "action.yaml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _action() -> str:
    return ACTION.read_text(encoding="utf-8")


def _step_run(name: str) -> str:
    workflow = yaml.safe_load(_workflow())
    return next(
        item["run"]
        for item in workflow["jobs"]["e2e"]["steps"]
        if item.get("name") == name
    )


def _parse_inputs(**changes: str) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(_workflow())
    step = next(
        item
        for item in workflow["jobs"]["prep"]["steps"]
        if item.get("name") == "Parse inputs"
    )
    match = re.search(r"<<'PY' >> \"\$GITHUB_OUTPUT\"\n(.*?)\n\s*PY", step["run"], re.S)
    assert match is not None
    environment = {
        **os.environ,
        "INPUT_RELEASES": "2608",
        "INPUT_RG": "",
        "INPUT_CLUSTER": "",
        "INPUT_UPGRADE_TO": "",
        "INPUT_SECRET_SYNC_MODES": "enabled",
        "INPUT_TESTS": "",
        "INPUT_PUBLISHED_RELEASE": "",
        "INPUT_PUBLISHED_SOURCE_SHA": "",
        "INPUT_SKIP_TEARDOWN": "false",
        "INPUT_KEEP_ALIVE": "0",
        "RUN_ID": "42",
        **changes,
    }
    return subprocess.run(
        [sys.executable, "-c", match.group(1)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _embedded_python(run: str) -> list[str]:
    return re.findall(r"<<'PY'\n(.*?)\n\s*PY(?:\n|$)", run, re.S)


def test_published_mode_is_explicit_and_bounded():
    workflow = _workflow()

    for value in (
        "published-release:",
        "published-source-sha:",
        "published-release and published-source-sha must be supplied together.",
        "Published E2E requires exactly one aio-releases entry.",
        "Published E2E requires an existing resource group",
        "Published E2E requires secret-sync-modes=disabled.",
        "Published E2E requires tests=aio-install.",
        "Published E2E cannot skip teardown.",
        "Published E2E cannot keep the cluster alive.",
    ):
        assert value in workflow


def test_published_input_contract_accepts_only_the_bounded_shape():
    result = _parse_inputs(
        INPUT_SECRET_SYNC_MODES="disabled",
        INPUT_TESTS="aio-install",
        INPUT_PUBLISHED_RELEASE="v0.0.4.dev20260919",
        INPUT_PUBLISHED_SOURCE_SHA="a" * 40,
        INPUT_RG="paymauntarget3",
    )

    assert result.returncode == 0, result.stderr
    assert "published_mode=true" in result.stdout
    assert "published_release=v0.0.4.dev20260919" in result.stdout
    assert f"published_source_sha={'a' * 40}" in result.stdout
    assert "max_parallel=1" in result.stdout
    assert "persistent=true" in result.stdout
    assert "rg_in=paymauntarget3" in result.stdout


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"INPUT_PUBLISHED_SOURCE_SHA": ""},
            "published-release and published-source-sha must be supplied together",
        ),
        (
            {"INPUT_PUBLISHED_RELEASE": "v0.0.4.dev20260919\npersistent=false"},
            "published-release must be bounded text without whitespace or controls",
        ),
        (
            {"INPUT_RELEASES": "2608\npersistent=false"},
            "aio-releases entry must be bounded text without whitespace or controls",
        ),
        (
            {"INPUT_RG": "paymauntarget3\npersistent=false"},
            "resource-group must be bounded text without whitespace or controls",
        ),
        (
            {"INPUT_CLUSTER": "name\npersistent=false"},
            "cluster-name must be bounded text without whitespace or controls",
        ),
        (
            {"INPUT_RELEASES": "2607,2608"},
            "requires exactly one aio-releases entry",
        ),
        (
            {"INPUT_RG": ""},
            "requires an existing resource group",
        ),
        (
            {"INPUT_CLUSTER": "named"},
            "requires an existing resource group",
        ),
        (
            {"INPUT_UPGRADE_TO": "2607"},
            "does not support the upgrade phase",
        ),
        (
            {"INPUT_SECRET_SYNC_MODES": "enabled"},
            "requires secret-sync-modes=disabled",
        ),
        (
            {"INPUT_TESTS": ""},
            "requires tests=aio-install",
        ),
        (
            {"INPUT_SKIP_TEARDOWN": "true"},
            "cannot skip teardown",
        ),
        (
            {"INPUT_KEEP_ALIVE": "1"},
            "cannot keep the cluster alive",
        ),
    ],
)
def test_published_input_contract_rejects_scope_expansion(changes, message):
    values = {
        "INPUT_SECRET_SYNC_MODES": "disabled",
        "INPUT_TESTS": "aio-install",
        "INPUT_PUBLISHED_RELEASE": "v0.0.4.dev20260919",
        "INPUT_PUBLISHED_SOURCE_SHA": "a" * 40,
        "INPUT_RG": "paymauntarget3",
        **changes,
    }
    result = _parse_inputs(**values)

    assert result.returncode != 0
    assert message in result.stderr


def test_source_input_contract_keeps_existing_parallel_default():
    result = _parse_inputs()

    assert result.returncode == 0, result.stderr
    assert "published_mode=false" in result.stdout
    assert "max_parallel=20" in result.stdout


def test_published_engine_is_authenticated_and_cannot_come_from_checkout():
    action = _action()

    for value in (
        "gh attestation verify",
        ".github/workflows/_siteops-distribution.yaml",
        ".github/workflows/release.yaml",
        "refs/heads/main",
        '\"runnerEnvironment\": \"self-hosted\"',
        "--source-digest \"$EXPECTED_SOURCE_SHA\"",
        "--signer-digest \"$EXPECTED_SOURCE_SHA\"",
        "--require-hashes",
        "--no-index",
        "$STATE/bundle/pylock.toml",
        "pipx==$PIPX_VERSION",
        "pipx\" install siteops",
        "pipx\" upgrade-shared",
        "Published E2E imported Site Ops from checkout.",
    ):
        assert value in action
    assert "pip install -e" not in action


def test_published_setup_finishes_before_azure_provisioning():
    workflow = _workflow()

    setup = workflow.index("uses: ./.github/actions/setup-published-siteops")
    project = workflow.index("- name: Prepare the published workspace project")
    cluster = workflow.index("uses: ./.github/actions/create-k3s-cluster")
    login = workflow.index("uses: azure/login@")
    plan = workflow.index("- name: Render and plan the published-package operator Site")
    connect = workflow.index("uses: ./.github/actions/connect-arc")
    assert setup < project < cluster < login < plan < connect


def test_published_workspace_uses_project_pin_and_separate_sites():
    workflow = _workflow()

    step = workflow[
        workflow.index("- name: Prepare the published workspace project"):
        workflow.index("uses: ./.github/actions/create-k3s-cluster")
    ]
    for value in (
        'mkdir \"$project\" \"$gh_config\"',
        "unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN",
        "project pin \"$project\"",
        "SITEOPS_REDACT_OUTPUT=0 siteops",
        '--source \"github:$GITHUB_REPOSITORY\"',
        '--release \"$RELEASE\"',
        "--release-workspace workspaces/iot-operations",
        "Project pin created operator Site content.",
    ):
        assert value in step
    assert 'mkdir "$project" "$cache"' not in step

    plan_step = workflow[
        workflow.index("- name: Render and plan the published-package operator Site"):
        workflow.index("- name: Preflight Arc cluster name is unused")
    ]
    for value in (
        "--template tests/e2e/sites/e2e-published.yaml.tmpl",
        'mkdir \"$SITEOPS_E2E_PROJECT/sites\"',
        "validate aio-install",
        "--plan",
        "--offline",
        "Published package planning changed the operator Site.",
    ):
        assert value in plan_step


def test_published_deploy_uses_only_the_pin_offline():
    workflow = _workflow()

    deploy = workflow[
        workflow.index("- name: Deploy AIO through the published engine and package"):
        workflow.index("- name: Observe bounded AIO readiness")
    ]
    for value in (
        '--project \"$SITEOPS_E2E_PROJECT\"',
        '--trust-policy \"$SITEOPS_E2E_POLICY\"',
        '--trusted-root \"$SITEOPS_E2E_TRUSTED_ROOT\"',
        "deploy aio-install",
        "--offline",
        '"apiVersion") != "siteops/v1alpha1"',
        f'"kind") != "{DEPLOYMENT_KIND}"',
        '"projection") != "publishable"',
        '"status") != "succeeded"',
        'result.get("engine", {}).get("version") != sys.argv[5]',
    ):
        assert value in deploy
    assert "workspaces/iot-operations" not in deploy


def test_published_deployment_receipt_accepts_the_real_run_envelope(tmp_path):
    blocks = _embedded_python(
        _step_run("Deploy AIO through the published engine and package")
    )
    assert len(blocks) == 1
    version = "1.0.0b1+build.42.1.g" + "a" * 12
    source = "a" * 40
    raw = tmp_path / "deployment.txt"
    output = tmp_path / "receipt.json"
    document = {
        "apiVersion": "siteops/v1alpha1",
        "kind": DEPLOYMENT_KIND,
        "projection": "publishable",
        "status": "succeeded",
        "exitCode": 0,
        "engine": {"name": "siteops", "version": version},
        "summary": {
            "sites": {"total": 1, "counts": {"succeeded": 1}},
            "operations": {"total": 5, "counts": {"succeeded": 5}},
        },
    }
    raw.write_text("safe progress\n" + json.dumps(document), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            blocks[0],
            str(raw),
            str(output),
            "v-test",
            source,
            version,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    receipt = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert receipt == {
        "apiVersion": "siteops.e2e/v1",
        "kind": "PublishedPackageDeployment",
        "release": "v-test",
        "sourceSha": source,
        "engineVersion": version,
        "siteCount": 1,
        "operationCount": 5,
        "succeededOperations": 5,
        "status": "succeeded",
    }

    document["kind"] = "DeploymentResult"
    raw.write_text(json.dumps(document), encoding="utf-8")
    rejected = subprocess.run(
        [
            sys.executable,
            "-c",
            blocks[0],
            str(raw),
            str(output),
            "v-test",
            source,
            version,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "deployment result is incomplete" in rejected.stderr


def test_default_source_e2e_remains_separate():
    workflow = _workflow()

    assert (
        "- uses: ./.github/actions/setup-siteops\n"
        "        if: needs.prep.outputs.published-mode != 'true'\n"
    ) in workflow
    assert (
        "- name: Run E2E integration tests\n"
        "        if: needs.prep.outputs.published-mode != 'true'\n"
    ) in workflow
    assert (
        "- uses: actions/upload-artifact@"
        in workflow
        and "always() && needs.prep.outputs.published-mode != 'true'" in workflow
    )


def test_published_readiness_is_bounded_and_existing_teardown_is_retained():
    workflow = _workflow()

    for value in (
        "microsoft.iotoperations/instances",
        "microsoft.deviceregistry/schemaregistries",
        "microsoft.deviceregistry/namespaces",
        "for _ in {1..60}",
        'status.get("phase") == "Running"',
        'condition.get("type") == "Ready"',
        "AIO instance projection and operator readiness did not converge within five minutes.",
        "PublishedPackageReadiness",
        "Snapshot RG resources (persistent mode)",
        "Teardown (persistent mode, delta cleanup, keep RG)",
        "comm -23",
        "operator-owned",
    ):
        assert value in workflow
