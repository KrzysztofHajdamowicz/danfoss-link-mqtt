FROM python:3.12-alpine3.24@sha256:b658b05ce9c4eb97ec6fc19424818a1f31993659c2b505a532f470e83807815d AS openssl-build
RUN apk add --no-cache build-base linux-headers patch perl
WORKDIR /build
COPY tools/fetch_openssl.py /build/fetch_openssl.py
RUN python fetch_openssl.py
COPY patches/openssl-3.5.9-bp320.patch /build/brainpool.patch
WORKDIR /build/openssl-3.5.9
RUN patch --batch --fuzz=0 -p1 < /build/brainpool.patch
# Keep the private crypto runtime inside this image. Upstream tested x86_64;
# aarch64 builds natively too (the brainpool patch is architecture-independent).
# Only the shared libraries and the CLI (used for identities and SKIs) are kept.
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64)  target=linux-x86_64 ;; \
      aarch64) target=linux-aarch64 ;; \
      *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    ./Configure "$target" --prefix=/opt/mdg-openssl --libdir=lib \
       --openssldir=/opt/mdg-openssl/ssl shared no-tests no-legacy no-docs; \
    nice -n 19 make -j"$(nproc)" build_sw; \
    make install_sw; \
    cd /opt/mdg-openssl; \
    rm -rf include lib/*.a lib/pkgconfig lib/cmake lib/engines-3 bin/c_rehash; \
    rmdir lib/ossl-modules 2>/dev/null || true; \
    strip --strip-unneeded lib/libssl.so.3 lib/libcrypto.so.3 bin/openssl

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# Locked runtime dependencies (uv.lock hashes) in a virtual environment.
FROM python:3.12-alpine3.24@sha256:b658b05ce9c4eb97ec6fc19424818a1f31993659c2b505a532f470e83807815d AS deps
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /build
COPY pyproject.toml uv.lock /build/
RUN uv sync --locked --no-dev --no-install-project --no-cache

FROM python:3.12-alpine3.24@sha256:b658b05ce9c4eb97ec6fc19424818a1f31993659c2b505a532f470e83807815d AS runtime
RUN apk add --no-cache tzdata
COPY --from=deps /opt/venv /opt/venv
COPY --from=openssl-build /opt/mdg-openssl /opt/mdg-openssl
COPY --from=openssl-build /build/openssl-3.5.9/apps/openssl.cnf /opt/mdg-openssl/ssl/openssl.cnf
COPY --from=openssl-build /build/openssl-3.5.9/LICENSE.txt /usr/share/licenses/openssl/LICENSE.txt
# Python's _ssl/_hashlib load the private OpenSSL instead of Alpine's.
ENV LD_LIBRARY_PATH=/opt/mdg-openssl/lib
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:/opt/mdg-openssl/bin:$PATH
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY src/ /app/
# Fails the build if the private runtime is not the one Python loads, or the source is broken.
RUN python -c "import ssl, hub_bridge, mqtt_bridge, addon_run, paho.mqtt.client; \
assert ssl.OPENSSL_VERSION.startswith('OpenSSL 3.5.9 '), ssl.OPENSSL_VERSION"

# Home Assistant add-on: runs as root and maps add-on options to the bridge.
FROM runtime AS addon
ARG BUILD_VERSION=dev
LABEL io.hass.type="addon" io.hass.version="${BUILD_VERSION}"
CMD ["python", "/app/addon_run.py"]

# Default target: standalone container, as in compose.yaml.
FROM runtime AS standalone
USER 65534:65534
CMD ["python", "hub_bridge.py", "--state", "/state/apk-profile"]
