import jenkins.model.Jenkins

// Job DSL reconciliation is intentionally environment-driven. Repository
// registrations are the source of truth and scans are requested through the
// gateway; this seed folder provides a stable namespace on a clean host.
def jenkins = Jenkins.get()
if (jenkins.getItem("repositories") == null) {
    jenkins.createProject(com.cloudbees.hudson.plugins.folder.Folder, "repositories")
}
jenkins.save()
