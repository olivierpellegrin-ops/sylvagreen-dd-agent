FROM python:3.12-slim

# Agent refinancement : rendu PDF WeasyPrint des demandes de refinancement.
# ca-certificates : le worker lit les certificats CA système (absents de l'image
# de base) ; libs Pango/Cairo/Fontconfig : WeasyPrint (PDF charte SylvaGreen).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
      libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 libcairo2 \
      libgdk-pixbuf-2.0-0 libharfbuzz0b libffi8 fontconfig fonts-dejavu-core \
      shared-mime-info \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir "mistralai-agents>=1.0,<2" "httpx>=0.27,<1" "pydantic>=2,<3" "pypdf>=4,<6" "weasyprint>=63" \
 && useradd --create-home --shell /usr/sbin/nologin agent \
 && chown -R agent:agent /app

USER agent
CMD ["agents", "run-worker"]
