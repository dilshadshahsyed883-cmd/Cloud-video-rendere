# Immutable runtime for SaladCloud amd64 NVIDIA nodes. No dependency installs at startup.
# Node from Debian Bullseye has a compatible older glibc for Ubuntu 22.04.
FROM node:20-bullseye-slim AS node
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04 AS editly-build
ENV DEBIAN_FRONTEND=noninteractive
COPY --from=node /usr/local/ /usr/local/
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python-is-python3 make g++ pkg-config ca-certificates git \
    libgl1-mesa-dev libglu1-mesa-dev libglew-dev libxi-dev libxext-dev libxrender-dev \
    libegl1-mesa-dev libgbm-dev libcairo2-dev libpango1.0-dev \
    libjpeg-dev libgif-dev librsvg2-dev libasound2-dev \
    && rm -rf /var/lib/apt/lists/*
RUN npm install --global --no-fund --no-audit editly@0.14.2 \
    && editly --help > /dev/null

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    LIBGL_ALWAYS_SOFTWARE=1 \
    PORT=8080 \
    RENDERER_DATA_DIR=/data/jobs \
    RENDERER_ENCODER=nvenc \
    RENDERER_STRICT_READY=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 ffmpeg xvfb xauth tini ca-certificates \
    libgl1 libglu1-mesa libglew2.2 libxi6 libxext6 libxrender1 libegl1 libgbm1 \
    libcairo2 libpango-1.0-0 libjpeg-turbo8 libgif7 librsvg2-2 libasound2 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=editly-build /usr/local/ /usr/local/
# Build-time capability check; actual NVENC test requires a node with NVIDIA drivers.
RUN ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_nvenc && editly --help >/dev/null
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin renderer \
    && mkdir -p /app /data/jobs && chown renderer:renderer /data/jobs
COPY --chown=renderer:renderer renderer /app/renderer
WORKDIR /app
USER renderer
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=4)" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "renderer.api"]
