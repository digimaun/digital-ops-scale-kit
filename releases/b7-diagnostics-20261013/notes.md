## Highlights

- Content and deployment failures carry stable diagnostic codes, so
  redacted CI logs name the cause without Site names, paths or values.
- `siteops validate --output json` emits a validation result. Deployment
  results add failure categories beside the existing counts.
- The Azure Pipelines templates and the GitHub reusable deploy workflow
  show the cause of a failed plan or run.
- Publication requires a passing acceptance run for the exact candidate,
  and published releases are immutable.
- The install guide documents AIO extension setting overrides for Sites.
- This is a qualification prerelease for the maintainer fork.