# syntax=docker/dockerfile:1
# Virtual players and capture tools for lab runs; the role is picked by the compose `command`.
# Build from the lab repo root: docker build -f images/players/Dockerfile .
FROM debian:trixie-slim

# gstreamer1.0-plugins-good carries the HTTP source and decoders gmediarender needs,
# which --no-install-recommends would otherwise leave out
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gmediarender gstreamer1.0-plugins-good squeezelite snapclient mpd ffmpeg \
        python3 python3-venv avahi-utils \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

COPY . /src
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m venv /opt/venv \
    && pip install "/src[players,scenarios,test]"
