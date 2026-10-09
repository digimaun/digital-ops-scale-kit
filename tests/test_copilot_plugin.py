"""Keep the Copilot plugin's manifest and skill commands aligned with the CLI."""

import json
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

from siteops import cli

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "siteops"
MARKETPLACE = Path(__file__).resolve().parents[1] / ".github" / "plugin" / "marketplace.json"
AGENT_PLUGINS_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MANIFEST_FIELDS = {
    "$schema", "name", "version", "description", "author", "homepage",
    "repository", "license", "keywords", "extensions",
}
ARM = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/plant/providers"
PLACEHOLDERS = {
    "<source>": "official",
    "<release>": "v1.0.0b7",
    "<owner>": "contoso",
    "<repository>": "content",
    "<cluster-resource-ID>": f"{ARM}/Microsoft.Kubernetes/connectedClusters/plant-one",
    "<instance-resource-ID>": f"{ARM}/Microsoft.IoTOperations/instances/plant-one",
}


def _skills():
    return sorted((PLUGIN / "skills").glob("*/SKILL.md"))


def _front_matter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path.parent.name} needs YAML front matter"
    return yaml.safe_load(match.group(1)), text[match.end():]


def _siteops_commands():
    commands = []
    for path in _skills():
        _, body = _front_matter(path)
        for block in re.findall(r"```text\n(.*?)```", body, re.DOTALL):
            commands += [line.strip() for line in block.splitlines() if line.strip().startswith("siteops ")]
    return commands


def _argv(command):
    def substitute(match):
        assert match.group(0) in PLACEHOLDERS, f"Add a sample value for {match.group(0)}."
        return PLACEHOLDERS[match.group(0)]

    return shlex.split(re.sub(r"<[^>]+>", substitute, command))


def test_manifest_uses_the_portable_plugin_format():
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["$schema"] == AGENT_PLUGINS_SCHEMA
    assert set(manifest) <= MANIFEST_FIELDS
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", manifest["name"])


def test_marketplace_lists_the_plugin_as_published():
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    (entry,) = [plugin for plugin in marketplace["plugins"] if plugin["name"] == manifest["name"]]
    assert (MARKETPLACE.parents[2] / entry["source"]).resolve() == PLUGIN
    assert (entry["version"], entry["description"]) == (manifest["version"], manifest["description"])


def test_each_skill_names_its_directory_and_says_when_to_use_it():
    assert _skills()
    for path in _skills():
        front, _ = _front_matter(path)
        assert front["name"] == path.parent.name
        assert "Use when" in front["description"]


@pytest.mark.parametrize("command", _siteops_commands())
def test_skill_commands_parse_with_the_current_cli(monkeypatch, command):
    class Parsed(Exception):
        pass

    def stop_after_parsing(verbose):
        raise Parsed

    monkeypatch.setattr(sys, "argv", _argv(command))
    monkeypatch.setattr(cli, "setup_logging", stop_after_parsing)
    with pytest.raises((Parsed, SystemExit)) as stopped:
        cli.main()
    # --version prints and exits successfully during parsing. A usage error exits with 2.
    if stopped.type is SystemExit:
        assert stopped.value.code == 0


def test_each_deploy_repeats_a_shown_plan_and_only_deploy_confirms():
    commands = [_argv(command) for command in _siteops_commands()]
    plans = [command[2:] for command in commands if command[1] == "plan"]
    deploys = [command for command in commands if command[1] == "deploy"]
    assert deploys
    for command in commands:
        assert ("--yes" in command) is (command[1] == "deploy")
    for deploy in deploys:
        assert [argument for argument in deploy[2:] if argument != "--yes"] in plans
