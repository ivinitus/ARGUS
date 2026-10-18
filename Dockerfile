FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/argus

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[video]" \
    && groupadd --gid 10001 argus \
    && useradd --uid 10001 --gid argus --create-home argus \
    && mkdir /data \
    && chown argus:argus /data

USER argus
WORKDIR /data
ENTRYPOINT ["argus"]
CMD ["--help"]
