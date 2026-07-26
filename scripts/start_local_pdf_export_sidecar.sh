#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$ROOT/.env"
IMAGE="peinidu-pdf-export:2.9.0-f8dffcf4"
CONTAINER="peinidu-pdf-export-local"

if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE" >&2
    exit 2
fi
if [ "$(stat -f '%Lp' "$ENV_FILE")" != "600" ]; then
    echo ".env must have mode 0600" >&2
    exit 2
fi

read_env_value() {
    awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE"
}

TOKEN=${PEINIDU_PDF_EXPORT_INTERNAL_TOKEN:-$(read_env_value PEINIDU_PDF_EXPORT_INTERNAL_TOKEN)}
if [ -z "$TOKEN" ]; then
    echo "PEINIDU_PDF_EXPORT_INTERNAL_TOKEN is required" >&2
    exit 2
fi

docker build -f "$ROOT/sidecar/pdf_export/Dockerfile" -t "$IMAGE" "$ROOT"
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    docker rm -f "$CONTAINER" >/dev/null
fi

docker run -d \
    --name "$CONTAINER" \
    --init \
    --user 10001:10001 \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --pids-limit 256 \
    --memory 4g \
    --cpus 2 \
    --stop-timeout 45 \
    --restart unless-stopped \
    --health-cmd "python /pet-sidecar/healthcheck.py" \
    --health-interval 30s \
    --health-timeout 5s \
    --health-retries 3 \
    --health-start-period 60s \
    -p 127.0.0.1:8091:8090 \
    --tmpfs /work:rw,nosuid,nodev,noexec,size=3g,uid=10001,gid=10001,mode=0700 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m,uid=10001,gid=10001,mode=0700 \
    -e HOME=/work/home \
    -e XDG_CACHE_HOME=/work/home/.cache \
    -e XDG_CONFIG_HOME=/work/home/.config \
    -e HF_HUB_OFFLINE=1 \
    -e TRANSFORMERS_OFFLINE=1 \
    -e PEINIDU_PDF_EXPORT_INTERNAL_TOKEN="$TOKEN" \
    -e PEINIDU_PDF_EXPORT_INTERNAL_BASE_URL=http://host.docker.internal:8000/internal/llm/v1 \
    -e PEINIDU_PDF_EXPORT_WORK_ROOT=/work/jobs \
    -e PEINIDU_PDF_EXPORT_MAX_FILE_BYTES=52428800 \
    -e PEINIDU_PDF_EXPORT_MAX_PAGES=200 \
    -e PEINIDU_PDF_EXPORT_CONCURRENCY=1 \
    -e PEINIDU_PDF_EXPORT_TIMEOUT_SECONDS=1800 \
    --entrypoint /bin/sh \
    "$IMAGE" \
    /pet-sidecar/entrypoint.sh python -m uvicorn app:app \
    --app-dir /pet-sidecar --host 0.0.0.0 --port 8090 >/dev/null

attempt=0
until docker exec "$CONTAINER" python /pet-sidecar/healthcheck.py; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "Local PDF export sidecar did not become healthy" >&2
        exit 1
    fi
    sleep 1
done

echo "Local PDF export sidecar is healthy at http://127.0.0.1:8091"
