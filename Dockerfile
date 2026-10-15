FROM python:3.12-slim AS openssl-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc libc6-dev make perl patch ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY tools/fetch_openssl.py /build/fetch_openssl.py
RUN python fetch_openssl.py
COPY patches/openssl-3.5.9-bp320.patch /build/brainpool.patch
WORKDIR /build/openssl-3.5.9
RUN patch --batch --fuzz=0 -p1 < /build/brainpool.patch
# Only x86_64 has been tested. Keep the private crypto runtime inside this image.
RUN test "$(uname -m)" = x86_64 \
    && ./Configure linux-x86_64 --prefix=/opt/mdg-openssl --libdir=lib \
       --openssldir=/opt/mdg-openssl/ssl shared no-tests no-legacy \
    && nice -n 19 make -j1 build_sw \
    && make install_sw

FROM python:3.12-slim
COPY --from=openssl-build /opt/mdg-openssl /opt/mdg-openssl
COPY --from=openssl-build /build/openssl-3.5.9/apps/openssl.cnf /opt/mdg-openssl/ssl/openssl.cnf
COPY --from=openssl-build /build/openssl-3.5.9/LICENSE.txt /usr/share/licenses/openssl/LICENSE.txt
ENV LD_LIBRARY_PATH=/opt/mdg-openssl/lib
ENV PATH=/opt/mdg-openssl/bin:$PATH
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ /app/
COPY tests/ /app/tests/
# Fails the build if an old source bundle has accidentally been substituted.
RUN python -c "from mqtt_bridge import clean_world, MQTTBridge"
USER 65534:65534
CMD ["python", "mqtt_bridge.py", "--state", "/state/apk-profile"]
