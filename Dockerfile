FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
RUN python -m pip install --upgrade "pip==25.1.1" "build==1.2.2.post1" \
    && python -m build --wheel

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
RUN groupadd --gid 10001 jenkinsservice \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent jenkinsservice \
    && python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --requirement /tmp/requirements.lock \
    && rm /tmp/requirements.lock
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-deps /tmp/*.whl && rm /tmp/*.whl
COPY examples /app/examples
COPY extensions /app/extensions
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["jenkins-service"]
