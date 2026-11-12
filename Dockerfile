FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

FROM python:3.12-slim AS builder

COPY --from=uv /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY . .
RUN uv sync --no-dev

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Librairies systeme pour WeasyPrint (rendu HTML -> PDF A4 des etudes)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 \
    libcairo2 libgdk-pixbuf-2.0-0 libharfbuzz0b libffi8 \
    fontconfig fonts-dejavu-core shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1001 --create-home worker && chown -R 1001 /app
USER 1001

CMD ["/app/.venv/bin/python", "-m", "entrypoints.worker"]
