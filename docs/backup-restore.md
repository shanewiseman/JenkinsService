# Backup and restore

Back up PostgreSQL and the Jenkins home volume while builds are quiesced.
DinD image cache and agent workspaces are disposable and should not be backed
up. Secrets require a separate encrypted operator backup.

```bash
./scripts/backup.sh ./backups/jenkinsservice-$(date +%F)
```

Restore onto the same reviewed release:

1. stop gateway, orchestrator, extension runner, and Jenkins
2. restore the Jenkins archive into an empty `jenkins-data` volume
3. start PostgreSQL and restore the custom-format dump
4. start Jenkins and confirm JCasC and zero controller executors
5. start remaining services and verify readiness, registrations, and audit
   continuity
6. trigger a non-production fixture pipeline

Never restore untrusted secrets or merge state from two independent hosts.
