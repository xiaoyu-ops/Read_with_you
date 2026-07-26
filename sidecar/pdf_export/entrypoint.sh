#!/bin/sh
set -eu

umask 077
source_cache=/opt/pdf-export-cache
runtime_home=/work/home

if [ ! -d "$source_cache" ]; then
    echo "Pinned image does not contain its preloaded cache." >&2
    exit 70
fi

mkdir -p "$runtime_home/.cache" /work/jobs
if [ ! -f "$runtime_home/.cache/.pet-preloaded" ]; then
    # PDFMathTranslate and BabelDOC write below Path.home()/.cache. Copy the
    # image's preloaded assets into tmpfs before Python starts so read_only
    # rootfs remains usable without silently downloading models again.
    cp -a "$source_cache/." "$runtime_home/.cache/"
    chmod -R u+rwX "$runtime_home/.cache"
    touch "$runtime_home/.cache/.pet-preloaded"
fi

export HOME="$runtime_home"
export XDG_CACHE_HOME="$runtime_home/.cache"
export XDG_CONFIG_HOME="$runtime_home/.config"
mkdir -p "$XDG_CONFIG_HOME"

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec python -m uvicorn app:app --app-dir /pet-sidecar --host 0.0.0.0 --port 8090
