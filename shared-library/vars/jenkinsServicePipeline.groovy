import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic

def call(Map settings = [:]) {
    if (!settings.repositoryUrl || !settings.trustedBranch || !settings.repository) {
        error('repositoryUrl, trustedBranch, and repository are required')
    }

    String gitleaksImage = 'ghcr.io/gitleaks/gitleaks:v8.28.0@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854'
    String trivyImage = 'aquasec/trivy:0.64.1@sha256:a8ca29078522f30393bdb34225e4c0994d38f37083be81a42da3a2a7e1488e9e'
    List checks = []
    boolean requiredFailure = false
    String buildLabel = "dev.jenkinsservice.build=${env.JOB_NAME}-${env.BUILD_NUMBER}".replaceAll('[^A-Za-z0-9_.=-]', '-')
    String buildNetwork = ''

    node('orchestrator') {
        try {
            stage('Contract validation') {
                deleteDir()
                dir('_trusted') {
                    checkout([
                        $class: 'GitSCM',
                        branches: [[name: settings.trustedBranch]],
                        userRemoteConfigs: [[
                            url: settings.repositoryUrl,
                            credentialsId: 'github-read-pat',
                            refspec: '+refs/heads/*:refs/remotes/origin/*'
                        ]],
                        extensions: [
                            [$class: 'CloneOption', shallow: true, depth: 1, noTags: true]
                        ]
                    ])
                }
                String raw = readFile('_trusted/.jenkins/pipeline.yaml')
                writeFile file: '_pipeline.yaml', text: raw
                sh '''
                    jenkins-service-contract _trusted/.jenkins/pipeline.yaml --json-output > _validation.json
                    python3 - <<'PY'
import json
from pathlib import Path

validation = json.loads(Path("_validation.json").read_text())
assert validation["valid"], validation["errors"]
Path("_pipeline.json").write_text(json.dumps(validation["normalized"]))
PY
                '''
            }

            Map contract = new JsonSlurperClassic().parseText(readFile('_pipeline.json')) as Map
            if ((contract.runtime.network ?: 'none') == 'egress') {
                buildNetwork = "jenkinsservice-${env.JOB_NAME}-${env.BUILD_NUMBER}".replaceAll('[^A-Za-z0-9_.-]', '-').take(63)
                sh "docker network create --label ${shellQuote(buildLabel)} ${shellQuote(buildNetwork)}"
            }
            int timeoutMinutes = (contract.runtime.timeoutMinutes ?: 30) as int
            timeout(time: timeoutMinutes, unit: 'MINUTES') {
                stage('Clean exact-SHA checkout') {
                    dir('source') {
                        deleteDir()
                        checkout([
                            $class: 'GitSCM',
                            branches: [[name: params.COMMIT_SHA]],
                            userRemoteConfigs: [[
                                url: settings.repositoryUrl,
                                credentialsId: 'github-read-pat',
                                refspec: '+refs/heads/*:refs/remotes/origin/* +refs/pull/*/head:refs/remotes/origin/pr/*'
                            ]],
                            extensions: [
                                [$class: 'CloneOption', shallow: false, noTags: true],
                                [$class: 'CheckoutOption', timeout: 10]
                            ]
                        ])
                        sh 'test "$(git rev-parse HEAD)" = "$COMMIT_SHA"'
                    }
                }

                stage('Coding standards') {
                    contract.steps.standards.each { step ->
                        Map outcome = runContractStep(step as Map, contract.runtime as Map, buildLabel, buildNetwork)
                        checks << outcome
                        requiredFailure = requiredFailure || (outcome.required && outcome.status == 'failed')
                    }
                }

                stage('Built-in security scan') {
                    checks << runScanner(
                        'gitleaks',
                        gitleaksImage,
                        'detect --source=/workspace --report-format sarif --report-path /workspace/artifacts/gitleaks.sarif --no-banner',
                        buildLabel
                    )
                    requiredFailure = requiredFailure || checks.last().status == 'failed'
                }

                stage('Dependency and SBOM scan') {
                    checks << runScanner(
                        'trivy',
                        trivyImage,
                        'fs --format sarif --output /workspace/artifacts/trivy.sarif --scanners vuln,misconfig,secret /workspace',
                        buildLabel
                    )
                    checks << runScanner(
                        'sbom',
                        trivyImage,
                        'fs --format cyclonedx --output /workspace/artifacts/sbom.cdx.json /workspace',
                        buildLabel
                    )
                    requiredFailure = requiredFailure || checks[-1].status == 'failed' || checks[-2].status == 'failed'
                }

                stage('Repository tests') {
                    contract.steps.tests.each { step ->
                        Map outcome = runContractStep(step as Map, contract.runtime as Map, buildLabel, buildNetwork)
                        checks << outcome
                        requiredFailure = requiredFailure || (outcome.required && outcome.status == 'failed')
                    }
                }

                stage('Custom actions') {
                    (contract.steps.custom ?: []).each { step ->
                        Map outcome = runContractStep(step as Map, contract.runtime as Map, buildLabel, buildNetwork)
                        checks << outcome
                        requiredFailure = requiredFailure || (outcome.required && outcome.status == 'failed')
                    }
                }

                stage('Extension hooks') {
                    echo "${(contract.extensions ?: []).size()} extension hook(s) recorded for gateway execution"
                }
            }

            stage('Results publication') {
                dir('source') {
                    sh 'mkdir -p artifacts'
                    sh '''
                        python3 - <<'PY'
import hashlib
import json
from pathlib import Path

artifacts = []
for root_name in ("artifacts", "dist"):
    root = Path(root_name)
    if not root.exists():
        continue
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.as_posix()
        artifacts.append({
            "name": relative,
            "path": relative,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        })
Path("../_artifacts.json").write_text(json.dumps(artifacts))
PY
                    '''
                    Map result = [
                        schema_version: 'ci.jenkinsservice.dev/result/v1',
                        repository: settings.repository,
                        commit_sha: params.COMMIT_SHA,
                        pull_request: params.PULL_REQUEST ? params.PULL_REQUEST as int : null,
                        status: requiredFailure ? 'failed' : 'passed',
                        started_at: new Date(currentBuild.startTimeInMillis).format("yyyy-MM-dd'T'HH:mm:ssXXX"),
                        completed_at: new Date().format("yyyy-MM-dd'T'HH:mm:ssXXX"),
                        checks: checks,
                        artifacts: new JsonSlurperClassic().parseText(readFile('../_artifacts.json')),
                        extension_runs: []
                    ]
                    writeFile file: 'artifacts/pipeline-result.json', text: JsonOutput.prettyPrint(JsonOutput.toJson(result))
                    junit allowEmptyResults: true, testResults: '**/junit*.xml'
                    archiveArtifacts allowEmptyArchive: false, artifacts: 'artifacts/**,dist/**', fingerprint: true
                    recordIssues enabledForFailure: true, sarif(pattern: 'artifacts/*.sarif')
                }
            }

            if (requiredFailure) {
                error('one or more required checks failed')
            }
        } finally {
            stage('Cleanup') {
                sh(
                    script: "docker ps -aq --filter 'label=${shellQuote(buildLabel)}' | xargs -r docker rm -f",
                    returnStatus: true
                )
                if (buildNetwork) {
                    sh(script: "docker network rm ${shellQuote(buildNetwork)}", returnStatus: true)
                }
                cleanWs deleteDirs: true, disableDeferredWipeout: true, notFailBuild: true
            }
        }
    }
}

private Map runContractStep(Map step, Map runtime, String buildLabel, String buildNetwork) {
    long started = System.nanoTime()
    Map environment = [:]
    environment.putAll((runtime.environment ?: [:]) as Map)
    environment.putAll((step.environment ?: [:]) as Map)
    int timeoutMinutes = (step.timeoutMinutes ?: runtime.timeoutMinutes ?: 30) as int
    int status = sh(
        script: "mkdir -p source/artifacts\n" +
            "timeout --foreground ${timeoutMinutes}m " +
            dockerCommand(
                runtime,
                step.command,
                step.workingDirectory ?: '.',
                environment,
                buildLabel,
                buildNetwork
            ),
        returnStatus: true
    )
    return [
        id: step.id,
        name: step.name ?: step.id,
        status: status == 0 ? 'passed' : ((step.required == false) ? 'warning' : 'failed'),
        required: step.required != false,
        exit_code: status,
        duration_seconds: (System.nanoTime() - started) / 1_000_000_000.0,
        reports: (step.reports ?: []).collect { it.path }
    ]
}

private Map runScanner(String id, String image, String arguments, String buildLabel) {
    long started = System.nanoTime()
    int status = sh(
        script: """
            mkdir -p source/artifacts
            docker run --rm --network none --read-only --cap-drop ALL \\
              --security-opt no-new-privileges --pids-limit 256 --memory 1g --cpus 1 \\
              --label ${shellQuote(buildLabel)} --tmpfs /tmp:rw,noexec,nosuid,size=256m \\
              --volume ${shellQuote(pwd() + '/source')}:/workspace:rw \\
              ${shellQuote(image)} ${arguments}
        """.stripIndent(),
        returnStatus: true
    )
    return [
        id: id,
        name: id,
        status: status == 0 ? 'passed' : 'failed',
        required: true,
        exit_code: status,
        duration_seconds: (System.nanoTime() - started) / 1_000_000_000.0,
        reports: ["artifacts/${id == 'sbom' ? 'sbom.cdx.json' : id + '.sarif'}"]
    ]
}

private String dockerCommand(
    Map runtime,
    def command,
    String workingDirectory,
    Map environment,
    String buildLabel,
    String buildNetwork
) {
    String commandText = command instanceof List
        ? command.collect { shellQuote(it.toString()) }.join(' ')
        : command.toString()
    String environmentArgs = environment.collect { key, value ->
        "--env ${shellQuote(key.toString() + '=' + value.toString())}"
    }.join(' ')
    String network = (runtime.network ?: 'none') == 'egress' ? buildNetwork : 'none'
    return """
        docker run --rm --network ${shellQuote(network)} --cap-drop ALL \\
          --security-opt no-new-privileges --pids-limit ${(runtime.pids ?: 512) as int} \\
          --memory ${shellQuote((runtime.memory ?: '2Gi').toString())} \\
          --cpus ${shellQuote((runtime.cpu ?: 2).toString())} \\
          --user "\$(id -u):\$(id -g)" \\
          --label ${shellQuote(buildLabel)} --tmpfs /tmp:rw,noexec,nosuid,size=256m \\
          --volume ${shellQuote(pwd() + '/source')}:/workspace:rw \\
          --workdir ${shellQuote('/workspace/' + workingDirectory)} ${environmentArgs} \\
          ${shellQuote(runtime.image.toString())} /bin/sh -eu -c ${shellQuote(commandText)}
    """.stripIndent()
}

private String shellQuote(String value) {
    return "'${value.replace("'", "'\"'\"'")}'"
}
