# Pipeline result format

Every pipeline archives `artifacts/pipeline-result.json`, validated against
`pipeline-result-v1.schema.json`. Its version is
`ci.jenkinsservice.dev/result/v1`.

The result includes repository, exact source commit, the trusted target-branch
commit that supplied the pipeline contract, optional pull request, overall
status, and timestamps. Each check has a stable ID, required flag, status,
exit code, duration, and report paths. Checks that did not execute use
`skipped`; they are never represented as passed.

Artifact entries include size and a SHA-256 digest; gateway-provided download
URLs point only to curated artifact endpoints and are never arbitrary
filesystem paths. Every AI-review attempt adds an `ai-review` check, references
its extension run by UUID, and attaches `artifacts/ai-review.json`. The
structured log records the model and prompt version, reviewed identities,
outcome, summary, findings, GitHub actions performed or omitted, failure
reason when applicable, and timing. It contains no hidden model reasoning.

For a non-PR build, the AI-review check and log outcome are `skipped`, and no
successful GitHub AI-review status is published. If a later PR uses the same
source SHA and the same trusted target SHA, JenkinsService may create a
PR-specific result that reuses the passed native checks and artifacts. The
enclosing `Build` identifies the source through `reused_from_build_id`; only
the previously missing PR review is executed.

Consumers must ignore unknown check IDs but reject unknown result schema
versions. A required check with a failed status always dominates a nominal
process exit of zero.
