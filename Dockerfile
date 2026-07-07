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
# cal-sync-magic is a private repo that build.sh stages into the build
# context. On a clean clone the [c] glob matches nothing, the
# requirements.txt anchor keeps COPY satisfied, and the calendar app is
# simply left out of the image (it's optional in settings too).
COPY requirements.txt cal-sync-magi[c] /opt/app/libs/cal-sync-magic/
COPY static /opt/app/static
COPY pigscanfly /opt/app/pigscanfly
COPY templates /opt/app/templates
COPY scripts/start-server.sh /opt/app/
COPY *.py /opt/app/
WORKDIR /opt/app/
RUN if [ -e /opt/app/libs/cal-sync-magic/setup.py ] \
      || [ -e /opt/app/libs/cal-sync-magic/setup.cfg ] \
      || [ -e /opt/app/libs/cal-sync-magic/pyproject.toml ]; then \
      pip install --no-cache-dir -e /opt/app/libs/cal-sync-magic; \
    else \
      echo "cal-sync-magic not in build context; calendar app disabled"; \
    fi
RUN chown -R www-data:www-data /opt/app

# start server
EXPOSE 80
STOPSIGNAL SIGTERM
CMD ["/opt/app/start-server.sh"]
