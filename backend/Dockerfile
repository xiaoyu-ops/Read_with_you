FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 peinidu \
    && useradd --uid 10001 --gid peinidu --create-home --shell /usr/sbin/nologin peinidu

ENV HOME=/home/peinidu

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend/public/mascot/home-mascot.png frontend/public/mascot/home-mascot.png
COPY scripts scripts
COPY config/config.example.yaml config/config.example.yaml
COPY docs/third-party/pdf-export-sidecar.md docs/third-party/pdf-export-sidecar.md
COPY sidecar/pdf_export/ pdf_export_wrapper_source/
COPY docker-compose.yml docker-compose.yml
COPY deploy/nginx.conf deploy/nginx.conf
RUN mkdir -p data config \
    && chown -R peinidu:peinidu /app /home/peinidu

USER peinidu

EXPOSE 8000

CMD ["uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
