"""Integration tests for the opc-ua-solution.yaml manifest."""

import pytest

from tests.integration.helpers.assertions import (
    assert_step_succeeded,
    find_step,
)
from tests.integration.conftest import WORKSPACE_PATH

pytestmark = [pytest.mark.integration]


class TestOpcUaSolutionDeployment:
    """Validate that opc-ua-solution.yaml deploys successfully."""

    def test_no_failures(self, opc_ua_solution_result):
        assert opc_ua_solution_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            site = opc_ua_solution_result["sites"][name]
            assert site["status"] == "success", f"Site '{name}' failed: {site.get('error')}"

    def test_opc_ua_solution_step_succeeds(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            assert_step_succeeded(opc_ua_solution_result, name, "opc-ua-solution")


class TestOpcUaSolutionConditionalSteps:
    """Validate that conditional steps are gated correctly."""

    def test_opc_plc_simulator_conditional(self, opc_ua_solution_result):
        """OPC PLC simulator should respect includeOpcPlcSimulator deploy option."""
        for name in opc_ua_solution_result["sites"]:
            step = find_step(opc_ua_solution_result, name, "opc-plc-simulator")
            assert step["status"] in ("success", "skipped")


class TestOpcUaSolutionIdempotency:
    """Validate that re-deploying produces the same results."""

    def test_redeploy_succeeds(self, orchestrator, selector, opc_ua_solution_result):
        result2 = orchestrator.deploy(
            manifest_path=WORKSPACE_PATH / "manifests" / "opc-ua-solution.yaml",
            selector=selector,
        )
        assert result2["summary"]["failed"] == 0
