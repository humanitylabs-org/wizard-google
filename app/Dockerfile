FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
ARG TARGETARCH
# gogcli (MIT, https://github.com/steipete/gogcli): the Google CLI the agent's commands run through.
# Pinned release, checked against the release's published SHA-256 for each architecture.
ARG GOG_VERSION=0.45.0
ARG GOG_SHA256_AMD64=aa39f6149d6a1482cd71b7baf21b9c7a4bf87fe9e9118439c4e97026063e915f
ARG GOG_SHA256_ARM64=6eb4734b3a14a4c96f63c9747d06495265f3a4518a17dec0a4a4a710c76bcb2b
RUN set -eu; arch="${TARGETARCH:-$(uname -m)}"; \
    case "$arch" in amd64|x86_64) arch=amd64; sum="$GOG_SHA256_AMD64";; arm64|aarch64) arch=arm64; sum="$GOG_SHA256_ARM64";; *) echo "unsupported $arch"; exit 1;; esac; \
    wget -q -O /tmp/gog.tgz "https://github.com/steipete/gogcli/releases/download/v${GOG_VERSION}/gogcli_${GOG_VERSION}_linux_${arch}.tar.gz"; \
    echo "$sum  /tmp/gog.tgz" | sha256sum -c -; \
    mkdir /tmp/gogx; tar -xzf /tmp/gog.tgz -C /tmp/gogx; install -m 0755 /tmp/gogx/gog /usr/local/bin/gog; rm -rf /tmp/gog.tgz /tmp/gogx; \
    /usr/local/bin/gog --version
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /app/requirements.lock
COPY app /app/app
COPY web /app/web
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
ENV DATA_DIR=/data PORT=18812 PYTHONDONTWRITEBYTECODE=1 GOG_BIN=/usr/local/bin/gog
EXPOSE 18812
CMD ["python", "/app/app/server.py"]
