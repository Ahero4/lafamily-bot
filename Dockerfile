FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/app/data/lafamily.sqlite

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt

RUN groupadd --gid 10001 lafamily \
    && useradd --uid 10001 --gid lafamily --no-create-home --home-dir /app lafamily \
    && install -d -o lafamily -g lafamily /app/data \
    && ln -s /app/data/backups /app/backups

COPY main.py backup.py pyproject.toml CHANGELOG.md ./
COPY lafamily/ ./lafamily/

USER 10001:10001
VOLUME ["/app/data"]
ENTRYPOINT ["sh", "-c", "mkdir -p /app/data/backups && exec \"$@\"", "--"]
CMD ["python", "main.py"]
