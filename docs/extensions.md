# Extension development

An extension is an OCI image plus a
`ci.jenkinsservice.dev/extension/v1` manifest and input/output schemas. The
manifest declares its immutable image digest, actions, compatible contract
versions, schema filenames, bounded resources, network policy, secret
references, and possible GitHub action types. Schema filenames are local
single-component JSON paths; both schemas are checked when the catalog loads,
and every invocation is validated before and after container execution.

The operator must place the extension on `EXTENSION_ALLOWLIST`; the allowlist
is empty by default. Both gateway and runner load canonical manifests; the
runner rejects a caller-supplied manifest that differs. V1 containers run
with a read-only root, no network, all capabilities dropped,
no-new-privileges, PID/CPU/memory/time limits, and JSON over standard
input/output.

The extension never receives the write PAT. It can request only action types
declared by its manifest. The gateway validates output, enforces the manifest,
executes permitted GitHub calls, and audits image digest, target, response ID,
and idempotency key. With the selected automatic-write policy, no human
approval occurs after those checks.

`extensions/reference-review` is provider-neutral and has no SDK dependency.
It demonstrates structured findings and requested-action output.
