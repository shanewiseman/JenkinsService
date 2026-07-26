FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
RUN python -m pip install --no-cache-dir "build==1.2.2.post1" \
    && python -m build --wheel

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
RUN groupadd --gid 10003 reviewbroker \
    && useradd --uid 10003 --gid 10003 --no-create-home --home-dir /nonexistent reviewbroker \
    && python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --requirement /tmp/requirements.lock \
    && rm /tmp/requirements.lock
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-deps /tmp/*.whl && rm /tmp/*.whl
USER 10003:10003
EXPOSE 8100
ENTRYPOINT ["uvicorn", "jenkins_service.review_broker:create_review_broker_app", "--factory", "--host", "0.0.0.0", "--port", "8100"]
