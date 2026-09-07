# Tempest Weather Station — LAN web dashboard
#
# IMPORTANT: run this container with host networking. The Tempest hub sends
# UDP *broadcasts*, and broadcasts do not cross Docker's bridge network, so a
# bridged container will start cleanly and then never receive a packet.
#
#   docker run -d --name tempest --network host \
#     -e TEMPEST_LAT=45.4215 -e TEMPEST_LON=-75.6972 \
#     -v /volume1/docker/tempest:/data tempest-dashboard
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Tempest Weather Dashboard" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY tempest_core.py tempest_server.py ./
COPY web ./web

# No pip install: the app is standard library only.
ENV TEMPEST_DATA_DIR=/data \
    TEMPEST_HTTP_PORT=8444 \
    TEMPEST_UDP_PORT=50222 \
    PYTHONUNBUFFERED=1

# The image runs unprivileged. compose overrides `user:` on hosts (Synology,
# most NAS boxes) where the bind-mounted data directory belongs to someone
# else — see docker-compose.yml.
RUN mkdir -p /data && chmod 777 /data && \
    useradd --system --uid 10001 tempest && \
    chown -R tempest /app
USER tempest

VOLUME ["/data"]
EXPOSE 8444/tcp 50222/udp

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,os,sys; \
url='http://127.0.0.1:%s/healthz' % os.environ.get('TEMPEST_HTTP_PORT','8444'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

CMD ["python3", "tempest_server.py"]
