#!/usr/bin/env bash
# Reconcile the image venv with the checkout at /src, then start Music Assistant through the
# official entrypoint so its jemalloc preload stays.
set -euo pipefail
PY=/app/venv/bin/python
PYAV_WHEELS=/usr/local/share/pyav-wheels
export UV_CACHE_DIR=/cache/uv
# same as the official image build: keep urllib3-future out of the urllib3 namespace
export URLLIB3_NO_OVERRIDE=1

uv pip install --python "$PY" --index-strategy unsafe-best-match \
    --no-binary urllib3-future -r /src/requirements_all.txt

# The PyPI av wheel bundles its own FFmpeg; the image's wheel links the system FFmpeg.
required=$("$PY" -c "import importlib.metadata as m; print(m.version('av'))")
wheel=$(ls "$PYAV_WHEELS"/av-*.whl)
available=$(basename "$wheel" | cut -d- -f2)
if [ "$required" != "$available" ]; then
    echo "lab: requirements resolve to av $required but the base image ships av $available;" \
        "this ref needs a newer base image" >&2
    exit 1
fi
uv pip install --python "$PY" --force-reinstall --no-deps "$wheel"

frontend_pin=$(grep -o '"music-assistant-frontend==[^"]*"' /src/pyproject.toml | tr -d '"')
if ! grep -qx "$frontend_pin" /src/requirements_all.txt; then
    uv pip install --python "$PY" "$frontend_pin"
fi

# /src is read-only, which rules out an editable install (it writes egg-info into the source)
export PYTHONPATH=/src
exec /usr/local/bin/entrypoint.sh --data-dir /data --cache-dir /data/.cache
