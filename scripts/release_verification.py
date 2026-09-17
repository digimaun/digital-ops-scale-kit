# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Create policy for one invocation from trusted release context, not package metadata."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from siteops.artifacts import ArtifactError, open_regular_file
from siteops.cache_layout import write_new
from siteops.github_attestation import (
    MAX_EVIDENCE_BYTES,
    load_github_policy,
    verify_github_artifact,
)


class ReleaseVerifier:
    def __init__(
        self,
        directory: Path,
        trusted_root: Path,
        source: dict[str, str],
        *,
        signer: str,
        builder: str,
    ):
        if set(source) != {"repository", "commit", "ref"}:
            raise ArtifactError(
                "Release verification requires a complete independent source identity."
            )
        directory.mkdir(mode=0o700)
        with open_regular_file(trusted_root) as stream:
            raw = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ArtifactError("The independent trusted-root snapshot exceeds its byte limit.")
        self.root = directory / "root.json"
        self.policy_file = directory / "policy.json"
        self.source = dict(source)
        write_new(self.root, raw)
        write_new(
            self.policy_file,
            json.dumps(
                {
                    "apiVersion": "siteops/v1alpha1",
                    "kind": "ArtifactVerificationPolicy",
                    "id": "release-qualification",
                    "version": 1,
                    "validUntil": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "trustedRootSha256": hashlib.sha256(raw).hexdigest(),
                    "provider": {
                        "kind": "github-attestation/v1",
                        "repository": source["repository"],
                        "sourceRef": source["ref"],
                        "signerWorkflow": signer,
                        "builderWorkflow": builder,
                    },
                }
            ).encode("utf-8"),
        )
        self.policy = load_github_policy(self.policy_file)

    def __call__(self, artifact, proof, identity):
        receipt = verify_github_artifact(
            artifact,
            proof,
            self.root,
            self.policy_file,
            expected_sha256=identity.sha256,
            source_commit=self.source["commit"],
        )
        if (
            receipt.policy_sha256 != self.policy.sha256
            or receipt.root_sha256 != self.policy.trusted_root_sha256
        ):
            raise ArtifactError("Release verification policy changed during qualification.")
        return receipt
