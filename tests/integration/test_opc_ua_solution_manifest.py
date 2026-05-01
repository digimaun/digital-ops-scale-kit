"""Integration tests for the opc-ua-solution sample manifest."""

import pytest

from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_step_succeeded,
    find_step,
)
from tests.integration.conftest import WORKSPACE_PATH

pytestmark = [pytest.mark.integration]

OPC_UA_SOLUTION_MANIFEST = WORKSPACE_PATH / "samples" / "opc-ua-solution" / "manifest.yaml"


class TestOpcUaSolutionDeployment:
    """Validate that samples/opc-ua-solution/manifest.yaml deploys successfully."""

    def test_no_failures(self, opc_ua_solution_result):
        assert opc_ua_solution_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            site = opc_ua_solution_result["sites"][name]
            assert site["status"] == "success", f"Site '{name}' failed: {site.get('error')}"

    def test_opc_ua_solution_step_succeeds(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            assert_step_succeeded(opc_ua_solution_result, name, "opc-ua-solution")

    def test_event_hub_outputs(self, opc_ua_solution_result):
        """Dataflow destination must be reachable; surface the Event Hub
        name/namespace that downstream consumers (e.g., tests, dashboards)
        key off. Catches template regressions where the output object
        shape changes silently."""
        for name in opc_ua_solution_result["sites"]:
            step = assert_step_succeeded(opc_ua_solution_result, name, "opc-ua-solution")
            event_hub = assert_output_exists(step, "eventHub")
            assert isinstance(event_hub, dict), (
                f"Site '{name}': eventHub output is not an object: {event_hub!r}"
            )
            for key in ("name", "namespace"):
                assert event_hub.get(key), (
                    f"Site '{name}': eventHub.{key} missing "
                    f"(keys: {sorted(event_hub.keys())})"
                )

    def test_resolved_extension_name_output(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            step = assert_step_succeeded(opc_ua_solution_result, name, "opc-ua-solution")
            assert_output_exists(step, "resolvedExtensionName")


class TestOpcUaSolutionSimulator:
    """Validate that the opc-plc-simulator step deploys successfully.

    The simulator is part of the sample's core layer and runs unconditionally
    when the sample is deployed.
    """

    def test_simulator_succeeds(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            step = find_step(opc_ua_solution_result, name, "opc-plc-simulator")
            assert step["status"] == "success", (
                f"Site '{name}': opc-plc-simulator status was {step['status']}"
            )


class TestOpcUaSolutionIdempotency:
    """Validate that re-deploying produces the same results."""

    def test_redeploy_preserves_outputs(
        self, orchestrator, selector, opc_ua_solution_result
    ):
        """Event Hub name/namespace must be stable across redeploys; a change
        indicates resources were recreated, which breaks any consumer that
        cached the endpoint."""
        result2 = orchestrator.deploy(
            manifest_path=OPC_UA_SOLUTION_MANIFEST,
            selector=selector,
        )
        assert result2["summary"]["failed"] == 0

        for name in opc_ua_solution_result["sites"]:
            step1 = find_step(opc_ua_solution_result, name, "opc-ua-solution")
            step2 = find_step(result2, name, "opc-ua-solution")
            eh1 = assert_output_exists(step1, "eventHub")
            eh2 = assert_output_exists(step2, "eventHub")
            assert eh1 == eh2, (
                f"Site '{name}': eventHub output changed on redeploy "
                f"({eh1!r} -> {eh2!r})"
            )
