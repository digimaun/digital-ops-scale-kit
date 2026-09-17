# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Freeze a verified built or published engine for workspace qualification."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_verification import ReleaseVerifier  # noqa: E402
from siteops_release import bind_prepared_plan, load_release_intent  # noqa: E402
from workspace_engine import prepare_engine  # noqa: E402

from siteops.cache_filesystem import make_private_directory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name, metavar, help_text in (
        ("repository", "REPOSITORY", "Source repository as owner/repository."),
        ("source-sha", "COMMIT", "Exact reviewed Git commit."),
        ("source-ref", "REF", "Full Git ref for the reviewed source."),
        ("release-file", "PATH", "Committed release.json path relative to the repository."),
        ("expected-plan-sha", "SHA256", "Independent SHA-256 of --prepared-plan."),
        ("builder-workflow", "PATH", "Repository path of the calling workflow approved for built engine provenance."),
    ):
        parser.add_argument("--" + name, required=True, metavar=metavar, help=help_text)
    for name, metavar, help_text in (
        ("root", "DIRECTORY", "Source repository containing the reviewed commit."),
        ("prepared-plan", "FILE", "Prepared release plan selecting the engine and workspaces."),
        ("output", "DIRECTORY", "New directory for verified engine assets and their selection record."),
        ("control", "DIRECTORY", "Private directory for verification policy snapshots and downloads."),
        ("trusted-root", "FILE", "Independently provisioned signing roots."),
    ):
        parser.add_argument("--" + name, required=True, type=Path, metavar=metavar, help=help_text)
    parser.add_argument("--built-assets", type=Path, metavar="DIRECTORY", help="Completed engine build assets. Omit for a referenced engine release.")
    parser.add_argument("--archive-sha", metavar="SHA256", help="Independent SHA-256 of siteops-install.zip in --built-assets.")
    parser.add_argument("--wheel-sha", metavar="SHA256", help="Independent SHA-256 of the standalone engine wheel in --built-assets.")
    parser.add_argument("--build-number", required=True, type=int, help="Candidate workflow run number.")
    parser.add_argument("--build-attempt", required=True, type=int, help="Candidate workflow run attempt.")
    parser.add_argument("--dry-run", action="store_true", help="Require preview identities and allow a committed release example.")
    args = parser.parse_args()
    try:
        intent = load_release_intent(
            args.root,
            args.source_sha,
            args.release_file,
            args.repository,
            args.source_ref,
            dry_run=args.dry_run,
        )
        plan_sha = bind_prepared_plan(intent, args.prepared_plan, args.expected_plan_sha)
        make_private_directory(args.control)
        transfers = args.control / "transfers"
        make_private_directory(transfers)
        checks = []

        def verify(artifact, proof, identity, source, builder):
            if not checks:
                checks.append(
                    ReleaseVerifier(
                        args.control / "policy",
                        args.trusted_root,
                        source,
                        signer=".github/workflows/_siteops-distribution.yaml",
                        builder=builder,
                    )
                )
            if checks[0].source != source:
                raise ValueError("The selected engine source changed during preparation.")
            return checks[0](artifact, proof, identity)

        selection = prepare_engine(
            intent,
            plan_sha,
            args.output,
            transfers,
            build_number=args.build_number,
            build_attempt=args.build_attempt,
            builder_workflow=args.builder_workflow,
            verifier=verify,
            built_assets=args.built_assets,
            archive_sha=args.archive_sha,
            wheel_sha=args.wheel_sha,
        )
    except (ValueError, OSError) as error:
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "Engine qualification inputs could not be prepared."
        )
        print(f"prepare-workspace-engine: {message}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "selectionSha256": hashlib.sha256(selection.serialized()).hexdigest(),
                "matrix": selection.matrix(),
                "engineVersion": selection.version,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
