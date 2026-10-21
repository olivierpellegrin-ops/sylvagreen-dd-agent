FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "mistralai==3.*" "httpx==0.28.*" "pydantic==2.*"

COPY worker.py dd_ingest.py ./

CMD ["python", "worker.py"]
