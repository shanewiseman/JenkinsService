FROM jenkins/jenkins:2.568.1-jdk21@sha256:f4f65e6cd1405cd889b7f5ac33f9d5cdc2a099de6b87fe8a3933b9c5d53d1d02

COPY --chown=jenkins:jenkins docker/jenkins/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

COPY --chown=jenkins:jenkins docker/jenkins/casc.yaml /usr/share/jenkins/ref/casc.yaml
COPY --chown=jenkins:jenkins docker/jenkins/jobs.groovy /usr/share/jenkins/ref/init.groovy.d/jobs.groovy
COPY --chown=jenkins:jenkins shared-library /usr/share/jenkins/ref/shared-library
RUN git -C /usr/share/jenkins/ref/shared-library init --initial-branch=main \
    && git -C /usr/share/jenkins/ref/shared-library config user.name JenkinsService \
    && git -C /usr/share/jenkins/ref/shared-library config user.email jenkinsservice@invalid \
    && git -C /usr/share/jenkins/ref/shared-library add . \
    && GIT_AUTHOR_DATE=2026-01-01T00:00:00Z \
       GIT_COMMITTER_DATE=2026-01-01T00:00:00Z \
       git -C /usr/share/jenkins/ref/shared-library commit -m "JenkinsService contract v1" \
    && git -C /usr/share/jenkins/ref/shared-library tag v1
