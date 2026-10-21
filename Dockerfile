FROM python:3.12-slim

WORKDIR /app

# SDK Workflows requis par les deployments manages Mistral (>= 3.10 pour le token service-account)
RUN pip install --no-cache-dir "mistralai-workflows>=3.10,<4" "httpx>=0.27,<1" "pydantic>=2,<3"

COPY worker.py dd_ingest.py ./

CMD ["python", "worker.py"]
