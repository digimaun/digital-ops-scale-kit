"""Assertion helpers for integration tests."""

from typing import Any


def find_step(result: dict[str, Any], site_name: str, step_name: str) -> dict[str, Any]:
    """Find a step result by site and step name.

    Args:
        result: Full deployment result from orchestrator.deploy()
        site_name: Name of the site
        step_name: Name of the step

    Returns:
        Step result dict with keys: step, status, outputs, error, reason

    Raises:
        KeyError: If site not found in results
        ValueError: If step not found for the site
    """
    site_result = result["sites"][site_name]
    for step in site_result["steps"]:
        if step["step"] == step_name:
            return step
    available = [s["step"] for s in site_result["steps"]]
    raise ValueError(f"Step '{step_name}' not found for site '{site_name}'. Available: {available}")


def assert_step_succeeded(result: dict[str, Any], site_name: str, step_name: str) -> dict[str, Any]:
    """Assert a step succeeded and return its result for further assertions."""
    step = find_step(result, site_name, step_name)
    assert step["status"] == "success", (
        f"Step '{step_name}' did not succeed for site '{site_name}': "
        f"status={step['status']}, error={step.get('error')}"
    )
    return step


def assert_step_skipped(result: dict[str, Any], site_name: str, step_name: str) -> dict[str, Any]:
    """Assert a step was skipped and return its result."""
    step = find_step(result, site_name, step_name)
    assert step["status"] == "skipped", (
        f"Step '{step_name}' was not skipped for site '{site_name}': status={step['status']}"
    )
    return step


def assert_output_exists(step_result: dict[str, Any], output_name: str) -> Any:
    """Assert an output exists in a step result and return its value.

    Handles both raw values and Azure ARM wrapped format {"value": X, "type": "..."}.
    """
    outputs = step_result.get("outputs", {})
    assert output_name in outputs, (
        f"Output '{output_name}' not found in step '{step_result['step']}'. "
        f"Available: {sorted(outputs.keys())}"
    )
    output = outputs[output_name]
    if isinstance(output, dict) and "value" in output:
        return output["value"]
    return output


def assert_output_starts_with(
    step_result: dict[str, Any], output_name: str, prefix: str
) -> str:
    """Assert an output value starts with the given prefix."""
    value = assert_output_exists(step_result, output_name)
    assert isinstance(value, str) and value.startswith(prefix), (
        f"Output '{output_name}' expected to start with '{prefix}', got: {value}"
    )
    return value
