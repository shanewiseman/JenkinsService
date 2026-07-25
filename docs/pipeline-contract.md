# Pipeline contract

`ci.jenkinsservice.dev/v1` is defined by
`src/jenkins_service/schemas/pipeline-v1.schema.json`. Each repository must
provide at least one standards step and one test step. Runtime images require
an `@sha256:` digest. Repository containers have no network by default. A
repository may explicitly select `runtime.network: egress`; Jenkins then
creates a uniquely named, build-labeled network and removes it during cleanup.
Use egress only for steps that must resolve locked dependencies.

Step IDs are stable result keys. A step declares a string or argument-list
command, a relative working directory, whether failure is required, optional
non-secret environment values, a timeout, and typed report paths. Absolute
paths, parent traversal, backslashes, secret-like environment names,
privileged flags, mounts, Docker socket references, and Docker client
configuration are rejected.

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
