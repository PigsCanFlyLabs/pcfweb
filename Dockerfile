# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1

# install nginx (+ curl for the GeoLite2 download below)
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY /conf/nginx.default /etc/nginx/sites-available/default
RUN ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log

# copy source and install dependencies
RUN mkdir -p /opt/app
RUN mkdir -p /opt/app/libs/cal-sync-magic
COPY requirements.txt /opt/app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /opt/app/requirements.txt

# GeoLite2 country database for region-specific buy links. Optional: pass
# the MaxMind license key as a BuildKit secret (see build.sh); without it
# the image still works, just without country detection.
ENV GEOIP_PATH=/opt/app/geoip
RUN --mount=type=secret,id=maxmind \
    mkdir -p /opt/app/geoip && \
    if [ -s /run/secrets/maxmind ]; then \
      curl -sSfL "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-Country&license_key=$(cat /run/secrets/maxmind)&suffix=tar.gz" \
        | tar -xz --strip-components=1 -C /opt/app/geoip; \
    else \
      echo "No maxmind secret provided; skipping GeoLite2 bundle (country detection disabled)"; \
    fi
COPY main /opt/app/main
COPY cal-sync-magic/*.py /opt/app/libs/cal-sync-magic
COPY cal-sync-magic/*.cfg /opt/app/libs/cal-sync-magic
COPY cal-sync-magic/*.ini /opt/app/libs/cal-sync-magic
COPY cal-sync-magic/cal_sync_magic /opt/app/libs/cal-sync-magic/cal_sync_magic
COPY static /opt/app/static
COPY pigscanfly /opt/app/pigscanfly
COPY templates /opt/app/templates
COPY scripts/start-server.sh /opt/app/
COPY *.py /opt/app/
WORKDIR /opt/app/
RUN pip install --no-cache-dir -e /opt/app/libs/cal-sync-magic
RUN chown -R www-data:www-data /opt/app

# start server
EXPOSE 80
STOPSIGNAL SIGTERM
CMD ["/opt/app/start-server.sh"]
