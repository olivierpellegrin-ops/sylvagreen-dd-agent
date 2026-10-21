FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "mistralai>=3.0" "httpx>=0.27" "pydantic>=2"

COPY worker.py dd_ingest.py ./

CMD ["python", "worker.py"]
