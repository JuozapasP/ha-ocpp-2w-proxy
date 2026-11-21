ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.24
FROM ${BUILD_FROM}

# Pinned upstream ocpp-2w-proxy. The patch below is applied to that exact commit.
ARG UPSTREAM_COMMIT=5e02d2ce3becb4c334c57e21f386906376ff537c
ARG UPSTREAM_REPO=https://github.com/ocpp-balanz/ocpp-2w-proxy.git

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

LABEL \
    org.opencontainers.image.title="OCPP Two-Way Proxy" \
    org.opencontainers.image.description="Proxy one OCPP 1.6J charger to two CSMS backends" \
    org.opencontainers.image.source="https://github.com/JuozapasP/ha-ocpp-2w-proxy" \
    org.opencontainers.image.licenses="MIT"

# cryptography and pydantic are listed in upstream requirements.txt but the
# proxy script never imports them. Only websockets is installed, pinned to the
# 15.x API this patch is tested against. That keeps the Alpine build free of a
# compiler. ca-certificates is required for the wss:// primary CSMS.
RUN set -eux \
    && apk add --no-cache ca-certificates git patch \
    && update-ca-certificates \
    && git -c init.defaultBranch=main init /tmp/ocpp-src \
    && git -C /tmp/ocpp-src remote add origin "${UPSTREAM_REPO}" \
    && git -C /tmp/ocpp-src fetch --depth 1 origin "${UPSTREAM_COMMIT}" \
    && git -C /tmp/ocpp-src checkout --detach FETCH_HEAD \
    && python3 -m pip install --no-cache-dir "websockets==15.0.1" \
    && mkdir -p /opt/ocpp-2w-proxy \
    && cp /tmp/ocpp-src/ocpp-2w-proxy.py /tmp/ocpp-src/LICENSE /opt/ocpp-2w-proxy/ \
    && rm -rf /tmp/ocpp-src \
    && apk del git

COPY patches/0001-ha-id-remap-and-pings.patch /tmp/ha.patch
RUN patch -p1 -d /opt/ocpp-2w-proxy -i /tmp/ha.patch \
    && rm /tmp/ha.patch \
    && apk del patch

COPY render_config.py /opt/ocpp-2w-proxy/render_config.py
COPY run.sh /run.sh
RUN chmod 0755 /run.sh

WORKDIR /opt/ocpp-2w-proxy
CMD ["/run.sh"]
