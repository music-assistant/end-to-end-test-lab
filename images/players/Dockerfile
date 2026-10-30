# syntax=docker/dockerfile:1
# Virtual players and capture tools for lab runs; the role is picked by the compose `command`.
# Build from the lab repo root: docker build -f images/players/Dockerfile .

# snapclient 0.31's file player runs a timer tick on the player it just destroyed at every stream
# change (client spins or crashes, no upstream fix through 0.35), so the lab patches and builds it
FROM debian:trixie-slim AS snapclient
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake patch ca-certificates libboost-dev libasound2-dev libpulse-dev \
        libflac-dev libvorbis-dev libopus-dev libsoxr-dev libavahi-client-dev libexpat1-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:d38d576f85bfa936412413b6860875ba3b462a8e67405f3984a0485778f2fdac \
    https://github.com/snapcast/snapcast/archive/refs/tags/v0.31.0.tar.gz /tmp/snapcast.tar.gz
COPY images/players/snapclient-file-player-alive.patch /tmp/
RUN mkdir /snapcast && tar xzf /tmp/snapcast.tar.gz -C /snapcast --strip-components=1 \
    && cd /snapcast && patch -p1 < /tmp/snapclient-file-player-alive.patch \
    && cmake -S . -B build -DBUILD_SERVER=OFF -DBUILD_CLIENT=ON -DBUILD_WITH_JACK=OFF \
        -DBUILD_WITH_TREMOR=OFF -DBUILD_TESTS=OFF -DCMAKE_BUILD_TYPE=Release \
    && make -C build -j4 snapclient

FROM debian:trixie-slim

# gstreamer1.0-plugins-good carries the HTTP source and decoders gmediarender needs,
# which --no-install-recommends would otherwise leave out
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gmediarender gstreamer1.0-plugins-good squeezelite snapclient mpd ffmpeg \
        python3 python3-venv avahi-utils \
    && rm -rf /var/lib/apt/lists/*
# the Debian package stays for its runtime libraries; /usr/local/bin shadows its binary
COPY --from=snapclient /snapcast/bin/snapclient /usr/local/bin/snapclient

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

COPY . /src
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m venv /opt/venv \
    && pip install "/src[players,scenarios,test]"
