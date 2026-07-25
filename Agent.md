# Agent operating contract

Read `IMPLEMENTATION_PLAN.md`, this file, and relevant documentation before
changing the service. Preserve the curated operation boundary, target-branch
trust for fork builds, immutable runtime/extension image requirements,
credential separation, webhook verification/deduplication, audit records,
and resource cleanup.

Never use a host Docker socket, expose Docker/PostgreSQL/agent ports, put
write PATs or Docker certificates in repository/extension containers, commit
secrets, or enable public release while the license is
`LicenseRef-Proprietary`.

Before handing off changes, run proportional unit/contract/static tests,
`docker compose config`, and package/image validation where available.
Document any acceptance test requiring operator credentials or hardware.
