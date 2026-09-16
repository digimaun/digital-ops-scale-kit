"""Complete non-AIO workspace inputs and the actual unsigned producer command."""

import shutil
import subprocess
import sys

from siteops.content_index import build_content_index, write_content_index
from siteops.github_catalog import github_input_digests
from tests.release_helpers import REPOSITORY, ROOT, SCRIPTS, SOURCE_REF

sys.path.insert(0, str(SCRIPTS))

from siteops_release import load_release_intent  # noqa: E402

PRODUCER = SCRIPTS / "build-workspace-release.py"


def _workspace(repository, name="workspace", *, index=False, github=False):
    root = repository / name
    shutil.copytree(ROOT / "tests" / "fixtures" / "browse-workspace", root)
    (repository / "LICENSE").write_text("Owned fixture license\n")
    if index:
        write_content_index(root, build_content_index(
            root, approve_public=True, additional_digests=github_input_digests if github else None,
        ))
    return {
        "workspace": name, "id": "fixture.storage", "package": name + ".zip",
        "compatibility": {"siteops": ">=1.2,<2"}, "licenses": ["LICENSE"],
    }


def _declaration(requests, *, combined=False):
    return {
        "tag": "v2.0.0b1" if combined else "v2.0.0",
        "siteops": {"build": True} if combined else {"release": "siteops/v1.2.3"},
        "workspaces": requests,
    }


def _load(repository, sha):
    return load_release_intent(
        repository, sha, "releases/candidate/release.json", REPOSITORY, SOURCE_REF,
    )


def _produce(repository, sha, output, *extra):
    return subprocess.run(
        [sys.executable, "-B", str(PRODUCER), "--root", str(repository),
         "--repository", REPOSITORY, "--expected-source-sha", sha,
         "--source-ref", SOURCE_REF, "--release-file", "releases/candidate/release.json",
         "--output-dir", str(output), *extra],
        cwd=output.parent, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        timeout=120, check=False,
    )
