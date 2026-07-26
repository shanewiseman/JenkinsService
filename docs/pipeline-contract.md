# Pipeline contract

`ci.jenkinsservice.dev/v1` is defined by
`src/jenkins_service/schemas/pipeline-v1.schema.json`. Each repository must
provide at least one standards step and one test step. Runtime images require
an `@sha256:` digest. Repository containers have no network by default. A step may explicitly set
`network: egress`; Jenkins then attaches only that step to a uniquely named,
build-labeled network and removes it during cleanup. `runtime.network` remains
a compatibility default, but new contracts should keep it `none` and grant
egress only to a trusted, locked dependency-bootstrap step.

Step IDs are stable result keys. A step declares a string or argument-list
command, a relative working directory, whether failure is required, optional
non-secret environment values, a timeout, and typed report paths. Absolute
paths, parent traversal, backslashes, secret-like environment names,
privileged flags, mounts, Docker socket references, and Docker client
configuration are rejected. Reports are typed as `junit`, `coverage`, `sarif`,
or `cyclonedx`. Optional `review` policy is PR-only, bounds the exact
base-to-head diff, and is locked to blocking only `critical` findings. Trusted
orchestration generates that diff from the checked-out Git objects and signs it
inside the replay-resistant completion callback; repository containers and the
review broker never receive the GitHub read PAT.

The trusted `review.excludedPaths` list may omit exact relative paths, such as
generated dependency lockfiles, from the diff sent to the AI review broker.
Entries are literal paths rather than glob patterns. Exclusion affects only AI
review input: checkout, tests, built-in security scans, dependency validation,
SBOM generation, and artifact publication continue to process the files.

The canonical result records the exact trusted-branch SHA that supplied the
contract. JenkinsService uses the source SHA and trusted SHA together as the
native-result reuse key. A later PR may reuse passed native checks for that
pair, but its missing review still runs against an identity-checked, bounded
PR diff. Checks that were not executed are recorded as `skipped`, never
`passed`.

The fixed stage order is:

1. contract validation
2. clean exact-SHA checkout
3. coding standards
4. mandatory Gitleaks scan
5. mandatory Trivy vulnerability/misconfiguration scan and CycloneDX SBOM
6. repository tests
7. custom actions
8. extension hooks
9. result publication
10. cleanup

Independent steps continue after failure where safe. Any failed required step
makes the final build fail. Only the trusted target branch supplies
orchestration and contract data for a fork pull request.
