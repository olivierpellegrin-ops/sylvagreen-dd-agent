FROM python:3.12-slim

# Le worker lit les certificats CA système (absents de l'image de base)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir "mistralai-agents>=1.0,<2" "httpx>=0.27,<1" "pydantic>=2,<3" \
 && useradd --create-home --shell /usr/sbin/nologin agent \
 && chown -R agent:agent /app

USER agent
CMD ["agents", "run-worker"]
