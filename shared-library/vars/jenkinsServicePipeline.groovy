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
    Map contract = [:]

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

            contract = new JsonSlurperClassic().parseText(readFile('_pipeline.json')) as Map
            List contractSteps = ['standards', 'tests', 'custom'].collectMany { group ->
                (contract.steps[group] ?: []) as List
            }
            boolean requiresEgress = (contract.runtime.network ?: 'none') == 'egress' ||
                contractSteps.any { step -> (step.network ?: 'none') == 'egress' }
            if (requiresEgress) {
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
                    checks << updateTrivyDatabase(trivyImage, buildLabel, buildNetwork)
                    if (checks.last().status == 'passed') {
                        checks << runTrivyScan(trivyImage, buildLabel)
                        checks << runScanner(
                            'sbom',
                            trivyImage,
                            'fs --offline-scan --skip-db-update --skip-dirs /workspace/.venv --skip-dirs /workspace/.ci-venv --skip-dirs /workspace/.ci-cache --format cyclonedx --output /workspace/artifacts/sbom.cdx.json /workspace',
                            buildLabel,
                            true
                        )
                    } else {
                        checks << failedCheck('trivy', 'Trivy vulnerability scan skipped: database update failed')
                        checks << failedCheck('sbom', 'CycloneDX generation skipped: database update failed')
                    }
                    requiredFailure = requiredFailure || checks[-1].status == 'failed' ||
                        checks[-2].status == 'failed' || checks[-3].status == 'failed'
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
                        base_sha: params.BASE_SHA ?: null,
                        build_id: params.BUILD_ID ?: null,
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
                    contractSteps.each { step ->
                        (step.reports ?: []).findAll { report -> report.type == 'coverage' }.each { report ->
                            recordCoverage(
                                id: step.id,
                                name: step.name ?: step.id,
                                sourceCodeRetention: 'NEVER',
                                tools: [[parser: 'COBERTURA', pattern: report.path]]
                            )
                        }
                    }
                    archiveArtifacts allowEmptyArchive: false, artifacts: 'artifacts/**,dist/**', fingerprint: true
                    recordIssues enabledForFailure: true, sarif(pattern: 'artifacts/*.sarif')
                }
            }

            if (requiredFailure) {
                error('one or more required checks failed')
            }
        } finally {
            int callbackStatus = 0
            stage('Cleanup') {
                String cleanupFilter = shellQuote("label=${buildLabel}")
                sh(
                    script: "docker ps -aq --filter ${cleanupFilter} | xargs -r docker rm -f",
                    returnStatus: true
                )
                if (buildNetwork) {
                    sh(script: "docker network rm ${shellQuote(buildNetwork)}", returnStatus: true)
                }
            }
            stage('Build completion callback') {
                callbackStatus = notifyBuildCompletion(contract, settings)
            }
            cleanWs deleteDirs: true, disableDeferredWipeout: true, notFailBuild: true
            if (callbackStatus != 0) {
                error('authenticated build completion callback failed after bounded retries')
            }
        }
    }
}

private int notifyBuildCompletion(Map contract, Map settings) {
    if (!env.BUILD_CALLBACK_URL || !params.BUILD_ID) {
        echo 'No build completion callback configured; skipping notification'
        return 0
    }
    Map result
    if (fileExists('source/artifacts/pipeline-result.json')) {
        result = new JsonSlurperClassic().parseText(
            readFile('source/artifacts/pipeline-result.json')
        ) as Map
    } else {
        result = [
            schema_version: 'ci.jenkinsservice.dev/result/v1',
            repository: settings.repository,
            commit_sha: params.COMMIT_SHA,
            base_sha: params.BASE_SHA ?: null,
            build_id: params.BUILD_ID,
            pull_request: params.PULL_REQUEST ? params.PULL_REQUEST as int : null,
            status: 'failed',
            started_at: new Date(currentBuild.startTimeInMillis).format("yyyy-MM-dd'T'HH:mm:ssXXX"),
            completed_at: new Date().format("yyyy-MM-dd'T'HH:mm:ssXXX"),
            checks: [],
            artifacts: [],
            extension_runs: []
        ]
    }
    Map review = (contract.review ?: [:]) as Map
    int maxDiffBytes = (review.maxDiffBytes ?: 200000) as int
    String reviewDiff = null
    if (review.enabled != false && params.PULL_REQUEST && params.BASE_SHA && fileExists('source/.git')) {
        int diffStatus = withEnv([
            "REVIEW_BASE_SHA=${params.BASE_SHA}",
            "REVIEW_HEAD_SHA=${params.COMMIT_SHA}",
            "REVIEW_MAX_DIFF_BYTES=${maxDiffBytes}"
        ]) {
            sh(
                script: """
                    set +x
                    python3 - <<'PY'
import os
import resource
import subprocess
from pathlib import Path

limit = int(os.environ["REVIEW_MAX_DIFF_BYTES"])
destination = Path("_review.diff")
destination.unlink(missing_ok=True)


def bound_output():
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit + 1, limit + 1))


try:
    with destination.open("wb") as output:
        completed = subprocess.run(
            [
                "git", "-C", "source", "diff", "--no-ext-diff", "--unified=3",
                os.environ["REVIEW_BASE_SHA"], os.environ["REVIEW_HEAD_SHA"], "--",
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            preexec_fn=bound_output,
        )
    if completed.returncode != 0 or destination.stat().st_size > limit:
        destination.unlink(missing_ok=True)
        raise SystemExit(1)
except (OSError, subprocess.SubprocessError):
    destination.unlink(missing_ok=True)
    raise SystemExit(1)
PY
                """.stripIndent(),
                returnStatus: true
            )
        }
        if (diffStatus == 0 && fileExists('_review.diff')) {
            reviewDiff = readFile(file: '_review.diff', encoding: 'UTF-8')
        } else {
            echo 'Trusted exact base-to-head diff was unavailable or exceeded its byte limit'
        }
    }
    Map payload = [
        callback_id: "${params.BUILD_ID}:${env.BUILD_NUMBER}",
        timestamp: System.currentTimeMillis().intdiv(1000),
        build_id: params.BUILD_ID,
        result: result,
        review: [
            enabled: review.enabled != false,
            pull_requests_only: true,
            critical_severities: review.criticalSeverities ?: ['critical'],
            max_diff_bytes: maxDiffBytes
        ],
        diff: reviewDiff
    ]
    writeFile file: '_build-completion.json', text: JsonOutput.toJson(payload)
    return sh(
        script: """
            set +x
            python3 - <<'PY'
import hashlib
import hmac
import os
import time
import urllib.request
from pathlib import Path

body = Path("_build-completion.json").read_bytes()
secret = Path("/run/secrets/build_callback_secret").read_bytes().strip()
signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
request = urllib.request.Request(
    os.environ["BUILD_CALLBACK_URL"],
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-JenkinsService-Signature": signature,
    },
    method="POST",
)
last_error = None
for attempt in range(3):
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status in {200, 202}:
                raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        if attempt < 2:
            time.sleep(2 ** attempt)
print(f"build completion callback failed: {type(last_error).__name__}")
raise SystemExit(1)
PY
        """.stripIndent(),
        returnStatus: true
    )
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
                buildNetwork,
                step.network ?: runtime.network ?: 'none'
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

private Map failedCheck(String id, String message) {
    echo message
    return [
        id: id,
        name: id,
        status: 'failed',
        required: true,
        exit_code: null,
        duration_seconds: 0.0,
        reports: []
    ]
}

private Map updateTrivyDatabase(String image, String buildLabel, String buildNetwork) {
    long started = System.nanoTime()
    String network = buildNetwork ?: 'bridge'
    int status = sh(
        script: """
            docker volume create jenkinsservice-trivy-cache >/dev/null
            docker run --rm --network ${shellQuote(network)} --cap-drop ALL \\
              --security-opt no-new-privileges --pids-limit 256 --memory 1g --cpus 1 \\
              --label ${shellQuote(buildLabel)} \\
              --volume jenkinsservice-trivy-cache:/root/.cache/trivy:rw \\
              ${shellQuote(image)} image --download-db-only
        """.stripIndent(),
        returnStatus: true
    )
    return [
        id: 'trivy-db',
        name: 'Trivy database update',
        status: status == 0 ? 'passed' : 'failed',
        required: true,
        exit_code: status,
        duration_seconds: (System.nanoTime() - started) / 1_000_000_000.0,
        reports: []
    ]
}

private Map runTrivyScan(String image, String buildLabel) {
    long started = System.nanoTime()
    String common = """
        docker run --rm --network none --read-only --cap-drop ALL \\
          --security-opt no-new-privileges --pids-limit 256 --memory 1g --cpus 1 \\
          --label ${shellQuote(buildLabel)} --tmpfs /tmp:rw,noexec,nosuid,size=256m \\
          --volume jenkinsservice-trivy-cache:/root/.cache/trivy:rw \\
          --volume ${shellQuote(pwd() + '/source')}:/workspace:rw \\
          ${shellQuote(image)} fs --offline-scan --skip-db-update --scanners vuln,misconfig,secret \\
          --skip-dirs /workspace/.venv --skip-dirs /workspace/.ci-venv --skip-dirs /workspace/.ci-cache
    """.stripIndent().trim()
    int reportStatus = sh(
        script: "mkdir -p source/artifacts\n${common} --format sarif " +
            "--output /workspace/artifacts/trivy.sarif /workspace",
        returnStatus: true
    )
    int gateStatus = sh(
        script: "${common} --severity HIGH,CRITICAL --exit-code 1 --format json " +
            "--output /workspace/artifacts/trivy-high-critical.json /workspace",
        returnStatus: true
    )
    int status = reportStatus == 0 && gateStatus == 0 ? 0 : 1
    return [
        id: 'trivy',
        name: 'Trivy HIGH/CRITICAL vulnerability gate',
        status: status == 0 ? 'passed' : 'failed',
        required: true,
        exit_code: status,
        duration_seconds: (System.nanoTime() - started) / 1_000_000_000.0,
        reports: ['artifacts/trivy.sarif', 'artifacts/trivy-high-critical.json']
    ]
}

private Map runScanner(
    String id,
    String image,
    String arguments,
    String buildLabel,
    boolean trivyCache = false
) {
    long started = System.nanoTime()
    String cacheArgument = trivyCache
        ? '--volume jenkinsservice-trivy-cache:/root/.cache/trivy:rw'
        : ''
    int status = sh(
        script: """
            mkdir -p source/artifacts
            docker run --rm --network none --read-only --cap-drop ALL \\
              --security-opt no-new-privileges --pids-limit 256 --memory 1g --cpus 1 \\
              --label ${shellQuote(buildLabel)} --tmpfs /tmp:rw,noexec,nosuid,size=256m \\
              --volume ${shellQuote(pwd() + '/source')}:/workspace:rw ${cacheArgument} \\
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
    String buildNetwork,
    String networkPolicy
) {
    String commandText = command instanceof List
        ? command.collect { shellQuote(it.toString()) }.join(' ')
        : command.toString()
    String environmentArgs = environment.collect { key, value ->
        "--env ${shellQuote(key.toString() + '=' + value.toString())}"
    }.join(' ')
    String network = networkPolicy == 'egress' ? buildNetwork : 'none'
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
