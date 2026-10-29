# syntax=docker/dockerfile:1
# Official server image plus the lab entrypoint, which runs the checkout mounted at /src.
# Build from this directory: docker build --build-arg BASE=<official image> -t lab/ma:<tag> .
ARG BASE
FROM ${BASE}

# git for requirements pinned to a git URL
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/lab-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/lab-entrypoint.sh"]
