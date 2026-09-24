# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import re
import shutil
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
        "INPUT_PUBLISHED_JOURNEY": "configured",
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


@pytest.mark.parametrize(("step", "minimum_python"), [
    ("Prepare guided AIO answers and plan", 5),
    ("Deploy AIO through the published engine and package", 1),
    ("Observe bounded AIO readiness", 3),
])
def test_guided_workflow_shell_and_embedded_python_parse_without_execution(
    step, minimum_python,
):
    if os.name == "nt":
        bash = Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe"
        if not bash.is_file():
            pytest.skip("Git Bash is needed for Windows shell syntax checks.")
    else:
        resolved = shutil.which("bash")
        if resolved is None:
            pytest.skip("Bash is needed for shell syntax checks.")
        bash = Path(resolved)
    run = _step_run(step)
    checked = subprocess.run(
        [str(bash), "-n"],
        input=run, capture_output=True, text=True, timeout=15, check=False,
    )
    assert checked.returncode == 0, checked.stderr
    blocks = _embedded_python(run)
    assert len(blocks) >= minimum_python
    for number, block in enumerate(blocks, start=1):
        compile(block, f"{step} embedded Python {number}", "exec")


def test_published_mode_is_explicit_and_bounded():
    workflow = _workflow()

    for value in (
        "published-release:",
        "published-source-sha:",
        "published-journey:",
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


def test_published_guided_journey_accepts_bounded_enabled_and_disabled_modes():
    result = _parse_inputs(
        INPUT_SECRET_SYNC_MODES="disabled,enabled",
        INPUT_TESTS="aio-install",
        INPUT_PUBLISHED_RELEASE="v0.0.4.dev20260919",
        INPUT_PUBLISHED_SOURCE_SHA="a" * 40,
        INPUT_PUBLISHED_JOURNEY="guided",
        INPUT_RG="paymauntarget3",
    )
    assert result.returncode == 0, result.stderr
    assert "published_journey=guided" in result.stdout
    assert 'secret_sync_modes=["disabled", "enabled"]' in result.stdout
    assert "max_parallel=1" in result.stdout


def test_guided_published_journey_is_not_inferred_from_source_checkout():
    result = _parse_inputs(INPUT_PUBLISHED_JOURNEY="guided")
    assert result.returncode != 0
    assert "published-journey requires published-release" in result.stderr


def test_guided_published_journey_pins_qualified_aio_version():
    result = _parse_inputs(
        INPUT_RELEASES="2607",
        INPUT_SECRET_SYNC_MODES="enabled",
        INPUT_TESTS="aio-install",
        INPUT_PUBLISHED_RELEASE="v0.0.4.dev20260919",
        INPUT_PUBLISHED_SOURCE_SHA="a" * 40,
        INPUT_PUBLISHED_JOURNEY="guided",
        INPUT_RG="paymauntarget3",
    )
    assert result.returncode != 0
    assert "Published guided E2E currently requires aio-releases=2608" in result.stderr


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

    assert 'if [[ "$PUBLISHED_JOURNEY" == "guided" ]]' in step
    assert 'export XDG_CONFIG_HOME="$RUNNER_TEMP/published-source-config"' in step
    assert '"XDG_CONFIG_HOME=$XDG_CONFIG_HOME" >> "$GITHUB_ENV"' in step
    assert 'source enroll guided --source "github:$GITHUB_REPOSITORY"' in step
    assert "if ! siteops \\\n" in step.split(
        'source enroll guided --source "github:$GITHUB_REPOSITORY"'
    )[0]
    assert 'trust_args=(--approved-source guided)' in step
    assert 'trust_args=(--trust-policy "$SITEOPS_E2E_POLICY"' in step
    assert '"${trust_args[@]}"' in step

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


def test_guided_published_plan_waits_for_arc_and_keeps_private_data_local():
    workflow = _workflow()
    connected = workflow.index("uses: ./.github/actions/connect-arc")
    guided = workflow.index("- name: Prepare guided AIO answers and plan")
    deployed = workflow.index("- name: Deploy AIO through the published engine and package")
    assert connected < guided < deployed
    step = _step_run("Prepare guided AIO answers and plan")
    for value in (
        "umask 077",
        "published-answers.json",
        "--project \"$SITEOPS_E2E_PROJECT\"",
        "--approved-source guided",
        "--read-resources",
        "--offline",
        "SITEOPS_REDACT_OUTPUT=0",
        "expected_steps",
        "read-required",
        "requirement-unmet",
    ):
        assert value in step
    assert "cat \"$RUNNER_TEMP/" not in step
    assert " -w workspaces/" not in step


def test_guided_published_deploy_uses_answers_and_read_gate():
    step = _step_run("Deploy AIO through the published engine and package")
    assert 'PUBLISHED_JOURNEY' in step
    assert '--input-file "$RUNNER_TEMP/published-answers.json"' in step
    assert "--read-resources" in step
    assert 'name=$SITE_NAME' in step
    assert 'trust_args=(--approved-source guided)' in step


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


def test_guided_enabled_readiness_requires_live_secret_sync_resources():
    step = _step_run("Observe bounded AIO readiness")
    for value in (
        "E2E_JOURNEY",
        "E2E_ENABLE_SECRET_SYNC",
        "microsoft.secretsynccontroller/azurekeyvaultsecretproviderclasses",
        "microsoft.managedidentity/userassignedidentities",
        "microsoft.keyvault/vaults",
        "az identity federated-credential list",
        "--api-version 2026-07-01",
        "defaultSecretProviderClassRef",
        "secretSyncEnabled",
    ):
        assert value in step


@pytest.mark.parametrize("enabled", [False, True])
def test_guided_readiness_receipt_asserts_enabled_state_without_private_ids(
    tmp_path, enabled,
):
    script = _embedded_python(_step_run("Observe bounded AIO readiness"))[-1]
    site = "private-site-name"
    spc_id = "/subscriptions/private/spc/private-name"
    resources = [
        {"type": "Microsoft.IoTOperations/instances", "name": "private-instance",
         "id": "/subscriptions/private/instance/private-name", "tags": {"site": site}},
        {"type": "Microsoft.DeviceRegistry/schemaRegistries"},
        {"type": "Microsoft.DeviceRegistry/namespaces"},
    ]
    if enabled:
        resources.extend([
            {"type": "Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses",
             "id": spc_id, "tags": {"site": site}},
            {"type": "Microsoft.ManagedIdentity/userAssignedIdentities", "tags": {"site": site}},
            {"type": "Microsoft.KeyVault/vaults", "tags": {"site": site}},
        ])
        (tmp_path / "published-federated.json").write_text(
            json.dumps([{"name": "private-credential"}]), encoding="utf-8",
        )
        (tmp_path / "published-aio-instance.json").write_text(
            json.dumps({"properties": {
                "defaultSecretProviderClassRef": {"resourceId": spc_id},
            }}),
            encoding="utf-8",
        )
    pods = {"items": [{
        "status": {"phase": "Running", "conditions": [
            {"type": "Ready", "status": "True"},
        ]},
    }]}
    instances = {"items": [{}]}
    for filename, document in (
        ("resources.json", resources),
        ("pods.json", pods),
        ("instances.json", instances),
    ):
        (tmp_path / filename).write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "readiness.json"
    environment = {
        "RUNNER_TEMP": str(tmp_path), "E2E_SITE_NAME": site,
        "E2E_JOURNEY": "guided",
        "E2E_ENABLE_SECRET_SYNC": "true" if enabled else "false",
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    result = subprocess.run(
        [
            sys.executable, "-c", script,
            str(tmp_path / "resources.json"),
            str(tmp_path / "pods.json"),
            str(tmp_path / "instances.json"),
            str(output),
        ],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["secretSyncEnabled"] is enabled
    assert spc_id not in json.dumps(receipt)
    assert site not in json.dumps(receipt)
    if enabled:
        assert receipt["spcBoundToInstance"] is True
        assert receipt["federatedCredentials"] == 1
    else:
        assert "federatedCredentials" not in receipt


@pytest.mark.parametrize("enabled", [False, True])
def test_guided_answers_use_a_private_file_with_no_implicit_site(tmp_path, enabled):
    block = _embedded_python(_step_run("Prepare guided AIO answers and plan"))[0]
    environment = {
        "RUNNER_TEMP": str(tmp_path),
        "E2E_SITE_NAME": "example-one",
        "E2E_SUBSCRIPTION": "00000000-0000-0000-0000-000000000001",
        "E2E_RESOURCE_GROUP": "rg-example",
        "E2E_CLUSTER_NAME": "arc-example",
        "E2E_AIO_RELEASE": "2608",
        "E2E_ENABLE_SECRET_SYNC": "true" if enabled else "false",
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    invoked = subprocess.run(
        [sys.executable, "-c", block],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    assert invoked.returncode == 0, invoked.stderr
    assert not invoked.stdout
    values = json.loads(
        (tmp_path / "published-answers.json").read_text(encoding="utf-8")
    )["values"]
    assert values["siteName"] == "example-one"
    assert values["cluster"].endswith("/connectedClusters/arc-example")
    assert values["enableSecretSync"] is enabled
    assert values["brokerMemoryProfile"] == "Low"
    assert all(values[key] is None for key in (
        "subscription", "resourceGroup", "location", "clusterName",
    ))
    if os.name == "posix":
        assert (tmp_path / "published-answers.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("enabled", [False, True])
def test_guided_private_plan_assertion_requires_expected_operations(
    tmp_path, enabled,
):
    block = _embedded_python(_step_run("Prepare guided AIO answers and plan"))[-1]
    expected_steps = {
        "global-edge-site": "skip", "edge-site": "skip",
        "schema-registry": "execute", "adr-ns": "execute",
        "aio-enablement": "execute", "aio-instance": "execute",
        "schema-registry-role": "execute",
        "resolve-aio": "execute" if enabled else "skip",
        "secretsync": "execute" if enabled else "skip",
    }
    plan = {
        "status": "planned", "executable": True,
        "engine": {"version": "test-build"},
        "plan": {
            "manifest": {"targetSelection": "explicit-site"},
            "submission": {"mode": "arm-json", "compilationBinding": "package-artifact"},
            "targets": [{"operations": [
                {"identity": {"step": name}, "disposition": disposition}
                for name, disposition in expected_steps.items()
            ]}],
        },
    }
    path = tmp_path / "published-guided-plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    environment = {
        "RUNNER_TEMP": str(tmp_path), "SITEOPS_E2E_ENGINE_VERSION": "test-build",
        "E2E_ENABLE_SECRET_SYNC": "true" if enabled else "false",
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    selected = subprocess.run(
        [sys.executable, "-c", block],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    assert selected.returncode == 0, selected.stderr
    plan["plan"]["targets"][0]["operations"][-1]["disposition"] = (
        "skip" if enabled else "execute"
    )
    path.write_text(json.dumps(plan), encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, "-c", block],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    assert rejected.returncode != 0
    assert "unexpected operations" in rejected.stderr
