"""Synthetic verified observations for closed tool doubles, not cryptographic evidence."""


def verified_observation(
    repository: str, commit: str, source_ref: str, signer: str, builder: str,
    runner: str = "self-hosted",
) -> dict:
    home = "https://github.com/" + repository
    return {"verificationResult": {
        "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
        "signature": {"certificate": {
            "subjectAlternativeName": home + "/" + signer + "@" + source_ref,
            "issuer": "https://token.actions.githubusercontent.com",
            "sourceRepositoryURI": home,
            "sourceRepositoryDigest": commit,
            "sourceRepositoryRef": source_ref,
            "buildSignerDigest": commit,
            "buildConfigURI": home + "/" + builder + "@" + source_ref,
            "buildConfigDigest": commit,
            "runnerEnvironment": runner,
        }},
    }}
