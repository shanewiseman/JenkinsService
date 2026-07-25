FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
RUN python -m pip install --no-cache-dir "build==1.2.2.post1" \
    && python -m build --wheel

FROM docker:29.6.2-cli@sha256:be132a9f282288de4afaf63379dff75711fda0147c6b72a9df44e51841402144 AS docker_cli

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/usr/local/bin:$PATH
RUN groupadd --gid 10002 runner \
    && useradd --uid 10002 --gid 10002 --no-create-home --home-dir /nonexistent runner \
    && python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --requirement /tmp/requirements.lock \
    && rm /tmp/requirements.lock
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-deps /tmp/*.whl && rm /tmp/*.whl
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY extensions /app/extensions
USER 10002:10002
EXPOSE 8090
ENTRYPOINT ["jenkins-service-extension-runner"]
