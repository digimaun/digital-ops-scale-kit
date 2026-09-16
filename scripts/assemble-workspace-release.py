# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Verify staged workspace proofs and produce the public routing descriptor."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from siteops_release import (  # noqa: E402
    ReleaseIntentError,
    bind_prepared_plan,
    load_release_intent,
)
from siteops_release_assets import ReleaseAssetsError  # noqa: E402
from workspace_release_assembly import assemble_workspace_assets  # noqa: E402

from siteops.artifacts import ArtifactError, hash_file  # noqa: E402
from siteops.github_attestation import (  # noqa: E402
    MAX_EVIDENCE_BYTES,
    load_github_policy,
    verify_github_artifact,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--release-file", required=True)
    parser.add_argument("--prepared-plan", required=True, type=Path)
    parser.add_argument("--expected-plan-sha", required=True)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--build-number", required=True, type=int)
    parser.add_argument("--build-attempt", required=True, type=int)
    parser.add_argument("--trust-policy", required=True, type=Path)
    parser.add_argument("--trusted-root", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        intent = load_release_intent(
            args.root, args.source_sha, args.release_file, args.repository,
            args.source_ref, dry_run=args.dry_run,
        )
        plan_sha = bind_prepared_plan(intent, args.prepared_plan, args.expected_plan_sha)
        for path in (args.trust_policy, args.trusted_root):
            if path.resolve().is_relative_to(args.staging.resolve()):
                raise ArtifactError("Workspace trust inputs must be independent of staged content.")
        policy = load_github_policy(args.trust_policy)
        if policy.repository != intent.repository or policy.source_ref != intent.source_ref:
            raise ArtifactError("The workspace verification policy does not approve the selected source.")
        if hash_file(args.trusted_root, limit=MAX_EVIDENCE_BYTES)[1] != policy.trusted_root_sha256:
            raise ArtifactError("The workspace trusted roots differ from the selected policy.")

        def verifier(artifact, proof, identity):
            receipt = verify_github_artifact(
                artifact, proof, args.trusted_root, args.trust_policy,
                expected_sha256=identity.sha256, source_commit=intent.source_sha,
            )
            if receipt.policy_sha256 != policy.sha256 or receipt.root_sha256 != policy.trusted_root_sha256:
                raise ArtifactError("Workspace verification policy changed during collection.")
            return receipt

        inventory = assemble_workspace_assets(
            intent, plan_sha, args.staging, args.output,
            build_number=args.build_number, build_attempt=args.build_attempt, verifier=verifier,
        )
    except (ReleaseIntentError, ReleaseAssetsError, ArtifactError, OSError) as error:
        message = str(error) if isinstance(error, ValueError) else "Workspace collection could not access its inputs or output."
        print(f"workspace-release: {message}", file=sys.stderr)
        return 1
    print(json.dumps({
        "inventorySha256": hashlib.sha256(inventory.serialized()).hexdigest(),
        "workspaces": len(intent.workspaces), "engineQualification": "not-established",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
