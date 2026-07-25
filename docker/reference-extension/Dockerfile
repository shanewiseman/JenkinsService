FROM python:3.12.11-alpine3.22@sha256:efcdfa6a6b2fd2afb9c7dfa9a5b288a6f68338b5cfdebe6b637d986067d85757
RUN addgroup -g 10003 extension \
    && adduser -D -u 10003 -G extension extension
COPY extensions/reference-review/review.py /app/review.py
USER 10003:10003
ENTRYPOINT ["python", "/app/review.py"]
