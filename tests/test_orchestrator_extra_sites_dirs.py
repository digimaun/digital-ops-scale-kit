"""Tests for the `extra_trusted_sites_dirs` orchestrator feature.

Covers:
- Behavioral: site discovery, inheritance, precedence, ordering.
- Validation: non-existent dirs, collisions with sites/ and sites.local/,
  de-duplication.
- CLI plumbing: `--extra-sites-dir` flag, `SITEOPS_EXTRA_SITES_DIRS`
  env var, precedence between them.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from siteops.cli import _resolve_extra_sites_dirs
from siteops.orchestrator import Orchestrator


def _write_site(path: Path, **overrides) -> None:
    """Write a minimal Site YAML file, applying overrides.

    Pass `key=None` in overrides to omit that default field entirely
    (useful when exercising inheritance).
    """
    data = {
        "apiVersion": "siteops/v1",
        "kind": "Site",
        "name": path.stem,
        "subscription": "00000000-0000-0000-0000-000000000000",
        "resourceGroup": f"rg-{path.stem}",
        "location": "eastus",
    }
    data.update(overrides)
    data = {k: v for k, v in data.items() if v is not None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


def _write_template(path: Path, **overrides) -> None:
    """Write a minimal SiteTemplate YAML file, applying overrides."""
    data = {
        "apiVersion": "siteops/v1",
        "kind": "SiteTemplate",
    }
    data.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


class TestExtraSitesDirsBehavior:
    """Behavioral tests for extra trusted site directories."""

    def test_site_in_extra_dir_is_discoverable(self, tmp_workspace, tmp_path):
        extra = tmp_path / "extra-sites"
        extra.mkdir()
        _write_site(extra / "remote-site.yaml")

        orchestrator = Orchestrator(tmp_workspace, extra_trusted_sites_dirs=[extra])

        site = orchestrator.load_site("remote-site")
        assert site.name == "remote-site"
        assert "remote-site" in {s.name for s in orchestrator.load_all_sites()}

    def test_site_in_extra_dir_honors_inherits(self, tmp_workspace, tmp_path):
        """Files in extra dirs are trusted: inherits is preserved."""
        extra = tmp_path / "extra-sites"
        extra.mkdir()
        shared = tmp_path / "shared"
        shared.mkdir()

        _write_template(
            shared / "base.yaml",
            subscription="inherited-sub",
            labels={"team": "platform"},
        )
        _write_site(
            extra / "child.yaml",
            subscription=None,
            inherits="../shared/base.yaml",
            labels={"environment": "prod"},
        )

        orchestrator = Orchestrator(tmp_workspace, extra_trusted_sites_dirs=[extra])
        site = orchestrator.load_site("child")

        assert site.subscription == "inherited-sub"
        assert site.labels["team"] == "platform"
        assert site.labels["environment"] == "prod"

    def test_inherits_target_resolved_relative_to_site_file(
        self, tmp_workspace, tmp_path
    ):
        """A site in sites/ can inherit a template located next to an extra dir."""
        extra = tmp_path / "extra-sites"
        extra.mkdir()
        _write_template(extra / "base.yaml", subscription="from-extra")

        # site in workspace sites/ inherits from the extra dir
        _write_site(
            tmp_workspace / "sites" / "local-child.yaml",
            subscription=None,
            inherits=str((extra / "base.yaml").resolve()),
        )

        orchestrator = Orchestrator(tmp_workspace, extra_trusted_sites_dirs=[extra])
        site = orchestrator.load_site("local-child")
        assert site.subscription == "from-extra"

    def test_sites_dir_wins_over_extra_on_same_name(self, tmp_workspace, tmp_path):
        """When a site name exists in both sites/ and an extra dir, sites/ wins
        as the base file (establishes the inheritance chain), but the extra
        dir file is still merged as an overlay with inherits stripped.
        """
        extra = tmp_path / "extra-sites"
        extra.mkdir()
        _write_site(
            tmp_workspace / "sites" / "dup.yaml",
            location="eastus",
            labels={"source": "primary"},
        )
        _write_site(
            extra / "dup.yaml",
            location="westus",
            labels={"source": "extra", "extra-only": "yes"},
        )

        orchestrator = Orchestrator(tmp_workspace, extra_trusted_sites_dirs=[extra])
        site = orchestrator.load_site("dup")

        # Extra dir overlays after sites/: its values win for overlapping keys.
        assert site.location == "westus"
        assert site.labels["source"] == "extra"
        # Keys only in extra are added.
        assert site.labels["extra-only"] == "yes"

    def test_multiple_extra_dirs_later_overrides_earlier(
        self, tmp_workspace, tmp_path
    ):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()

        _write_site(first / "shared.yaml", location="eastus")
        _write_site(second / "shared.yaml", location="westeurope")

        orchestrator = Orchestrator(
            tmp_workspace, extra_trusted_sites_dirs=[first, second]
        )
        site = orchestrator.load_site("shared")

        # 'second' is listed after 'first' so it overlays on top.
        assert site.location == "westeurope"

    def test_sites_local_still_wins_over_extras(self, tmp_workspace, tmp_path):
        extra = tmp_path / "extra-sites"
        extra.mkdir()
        _write_site(tmp_workspace / "sites" / "layered.yaml", location="eastus")
        _write_site(extra / "layered.yaml", location="westus")

        (tmp_workspace / "sites.local").mkdir()
        (tmp_workspace / "sites.local" / "layered.yaml").write_text(
            yaml.dump({"location": "northeurope"})
        )

        orchestrator = Orchestrator(tmp_workspace, extra_trusted_sites_dirs=[extra])
        site = orchestrator.load_site("layered")
        assert site.location == "northeurope"

    def test_sites_local_inherits_still_stripped_with_extras_present(
        self, tmp_workspace, tmp_path
    ):
        """The security invariant on sites.local/ must not be weakened by
        the presence of extra trusted directories.
        """
        extra = tmp_path / "extra-sites"
        extra.mkdir()
        shared = tmp_path / "shared"
        shared.mkdir()

        _write_template(shared / "evil.yaml", subscription="hijacked")
        _write_site(tmp_workspace / "sites" / "victim.yaml", subscription="original")

        (tmp_workspace / "sites.local").mkdir()
        (tmp_workspace / "sites.local" / "victim.yaml").write_text(
            yaml.dump({"inherits": str((shared / "evil.yaml").resolve())})
        )

        orchestrator = Orchestrator(tmp_workspace, extra_trusted_sites_dirs=[extra])
        site = orchestrator.load_site("victim")

        # inherits on sites.local/ is ignored: base chain stays with sites/.
        assert site.subscription == "original"

    def test_site_template_in_extra_dir_excluded_from_listing(
        self, tmp_workspace, tmp_path
    ):
        extra = tmp_path / "extra-sites"
        extra.mkdir()
        _write_template(extra / "base.yaml", subscription="tmpl")
        _write_site(extra / "real.yaml")

        orchestrator = Orchestrator(tmp_workspace, extra_trusted_sites_dirs=[extra])
        names = {s.name for s in orchestrator.load_all_sites()}
        assert "real" in names
        assert "base" not in names


class TestExtraSitesDirsValidation:
    """Constructor-time validation of extra_trusted_sites_dirs."""

    def test_nonexistent_dir_raises(self, tmp_workspace, tmp_path):
        missing = tmp_path / "does-not-exist"
        with pytest.raises(FileNotFoundError):
            Orchestrator(tmp_workspace, extra_trusted_sites_dirs=[missing])

    def test_file_instead_of_dir_raises(self, tmp_workspace, tmp_path):
        f = tmp_path / "just-a-file.txt"
        f.write_text("hello")
        with pytest.raises(FileNotFoundError):
            Orchestrator(tmp_workspace, extra_trusted_sites_dirs=[f])

    def test_workspace_sites_dir_rejected(self, tmp_workspace):
        with pytest.raises(ValueError, match="sites/"):
            Orchestrator(
                tmp_workspace,
                extra_trusted_sites_dirs=[tmp_workspace / "sites"],
            )

    def test_workspace_sites_local_dir_rejected(self, tmp_workspace):
        (tmp_workspace / "sites.local").mkdir()
        with pytest.raises(ValueError, match="sites.local/"):
            Orchestrator(
                tmp_workspace,
                extra_trusted_sites_dirs=[tmp_workspace / "sites.local"],
            )

    def test_duplicates_deduplicated_preserving_order(self, tmp_workspace, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        # Provide duplicates including a path spelled differently.
        orchestrator = Orchestrator(
            tmp_workspace,
            extra_trusted_sites_dirs=[a, b, a, Path(str(a))],
        )
        # Only [a, b] should remain, in that order.
        assert orchestrator._extra_trusted_sites_dirs == [a.resolve(), b.resolve()]


class TestCliExtraSitesDirsResolution:
    """Tests for CLI flag + env var resolution logic."""

    def test_cli_flag_only(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SITEOPS_EXTRA_SITES_DIRS", None)
            assert _resolve_extra_sites_dirs([d]) == [d]

    def test_env_var_only_parsed_with_pathsep(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        env_val = os.pathsep.join([str(a), str(b)])
        with patch.dict(os.environ, {"SITEOPS_EXTRA_SITES_DIRS": env_val}):
            result = _resolve_extra_sites_dirs(None)
        assert result == [a, b]

    def test_env_var_empty_segments_tolerated(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        # Leading / trailing / doubled separators should all be skipped.
        env_val = os.pathsep + str(a) + os.pathsep + os.pathsep
        with patch.dict(os.environ, {"SITEOPS_EXTRA_SITES_DIRS": env_val}):
            assert _resolve_extra_sites_dirs(None) == [a]

    def test_cli_wins_over_env(self, tmp_path, caplog):
        cli_dir = tmp_path / "cli"
        env_dir = tmp_path / "env"
        cli_dir.mkdir()
        env_dir.mkdir()
        with patch.dict(
            os.environ, {"SITEOPS_EXTRA_SITES_DIRS": str(env_dir)}
        ), caplog.at_level("INFO", logger="siteops.cli"):
            result = _resolve_extra_sites_dirs([cli_dir])
        assert result == [cli_dir]
        assert any(
            "SITEOPS_EXTRA_SITES_DIRS" in rec.message for rec in caplog.records
        )

    def test_neither_provided_returns_empty(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SITEOPS_EXTRA_SITES_DIRS", None)
            assert _resolve_extra_sites_dirs(None) == []
