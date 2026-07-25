# Release checklist

Releases are blocked while the project license is
`LicenseRef-Proprietary`. `scripts/release-guard.sh` and the release workflow
must fail until maintainers select and document a redistribution license.

After licensing is resolved:

1. update changelog and semantic version
2. refresh pinned Python dependencies, plugin locks, base/runtime/scanner
   digests, and extension digests
3. run unit, contract, integration, concurrent, security, backup/restore, and
   operator-credential E2E suites
4. validate clean-host bootstrap and Compose/JCasC configuration
5. build distributions and OCI images; generate CycloneDX SBOMs and
   vulnerability reports
6. sign artifacts/images and publish provenance
7. deploy to staging, test rollback, then obtain maintainer approval
8. tag the reviewed commit and publish immutable artifacts
