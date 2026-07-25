FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS contract_builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
RUN python -m pip install --no-cache-dir "build==1.2.2.post1" \
    && python -m build --wheel

FROM jenkins/inbound-agent:3355.v388858a_47b_33-2-jdk21@sha256:8eda4fe2a66bcf6a5e43436d9918fc14c306204dc8fcd75f4e15e0e6e5dc759a

USER root
ENV PATH=/opt/jenkinsservice/bin:$PATH
COPY --from=docker:29.6.2-cli@sha256:be132a9f282288de4afaf63379dff75711fda0147c6b72a9df44e51841402144 /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        curl \
        git \
        jq \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/jenkinsservice
COPY requirements.lock /tmp/requirements.lock
COPY --from=contract_builder /build/dist/*.whl /tmp/
RUN /opt/jenkinsservice/bin/pip install --no-cache-dir --requirement /tmp/requirements.lock \
    && /opt/jenkinsservice/bin/pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm /tmp/requirements.lock /tmp/*.whl
COPY --chown=jenkins:jenkins docker/agent/start-agent.sh /usr/local/bin/start-agent
USER jenkins
ENTRYPOINT ["/usr/local/bin/start-agent"]
