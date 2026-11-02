# Phasmid runtime image: the release build, its system libraries, Xvfb for
# headful mode, and a CDP endpoint on port 9222.
#
# From a GitHub release:
#   docker build -t phasmid docker/
# From a local directory holding phasmid-<version>-linux-x64.tar.zst:
#   docker build -t phasmid --build-context dist=<dir> \
#       --build-arg PHASMID_VERSION=<version> docker/

ARG PHASMID_VERSION=155.0.8059.84-p1

FROM scratch AS dist
ARG PHASMID_VERSION
ADD https://github.com/tintty1/phasmid/releases/download/${PHASMID_VERSION}/phasmid-${PHASMID_VERSION}-linux-x64.tar.zst /

FROM debian:trixie-slim AS unpack
ARG PHASMID_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends zstd \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dist /phasmid-${PHASMID_VERSION}-linux-x64.tar.zst /tmp/
RUN mkdir /opt/phasmid \
    && tar --zstd -xf /tmp/phasmid-${PHASMID_VERSION}-linux-x64.tar.zst -C /opt/phasmid --strip-components=1

FROM debian:trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        fonts-freefont-ttf \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        libasound2t64 \
        libatk-bridge2.0-0t64 \
        libatk1.0-0t64 \
        libatspi2.0-0t64 \
        libcairo2 \
        libcups2t64 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libfontconfig1 \
        libgbm1 \
        libglib2.0-0t64 \
        libgtk-3-0t64 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libudev1 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxkbcommon0 \
        libxrandr2 \
        libxrender1 \
        socat \
        tini \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 phasmid
COPY --from=unpack /opt/phasmid /opt/phasmid
COPY phasmid-chrome /usr/local/bin/phasmid-chrome
COPY entrypoint.sh /usr/local/bin/phasmid
USER phasmid
WORKDIR /home/phasmid
EXPOSE 9222
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/phasmid"]
