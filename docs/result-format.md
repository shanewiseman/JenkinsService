# Pipeline result format

Every pipeline archives `artifacts/pipeline-result.json`, validated against
`pipeline-result-v1.schema.json`. Its version is
`ci.jenkinsservice.dev/result/v1`.

The result includes repository, exact commit, optional pull request, overall
status and timestamps. Each check has a stable ID, required flag, status,
exit code, duration, and report paths. Artifact entries include size and a
SHA-256 digest; gateway-provided download URLs point to curated Jenkins
artifact endpoints and are never arbitrary filesystem paths. Extension runs
are referenced by UUID.

Consumers must ignore unknown check IDs but reject unknown result schema
versions. A required check with a failed status always dominates a nominal
process exit of zero.
