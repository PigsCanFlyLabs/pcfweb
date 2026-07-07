# syntax=docker/dockerfile:1

# GeoLite2 country database for region-specific buy links, fetched once on
# the build platform (the .mmdb is arch-independent, so this avoids one
# download per target arch and MaxMind's daily rate limit). Optional: pass
# the MaxMind license key as a BuildKit secret (see build.sh); without it
# the stage produces an empty directory and country detection is disabled.
FROM --platform=$BUILDPLATFORM python:3.13-slim-bookworm AS geoip
RUN --mount=type=secret,id=maxmind <<'EOF'
set -e
mkdir -p /geoip
if [ -s /run/secrets/maxmind ]; then
python3 - <<'PY'
import io
import tarfile
import urllib.request

key = open("/run/secrets/maxmind").read().strip()
url = ("https://download.maxmind.com/app/geoip_download"
       f"?edition_id=GeoLite2-Country&license_key={key}&suffix=tar.gz")
buf = io.BytesIO(urllib.request.urlopen(url).read())
with tarfile.open(fileobj=buf, mode="r:gz") as tf:
    for member in tf.getmembers():
        if member.name.endswith(".mmdb"):
            member.name = member.name.rsplit("/", 1)[-1]
            tf.extract(member, "/geoip")
PY
else
  echo "No maxmind secret provided; skipping GeoLite2 bundle (country detection disabled)"
fi
EOF


FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1

# install nginx
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY conf/nginx.default /etc/nginx/sites-available/default
RUN ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log

# The app dir itself stays www-data-writable (settings can materialize the
# Google client secret file there when configured via env).
RUN mkdir -p /opt/app /opt/app/media && chown www-data:www-data /opt/app /opt/app/media
COPY requirements.txt /opt/app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /opt/app/requirements.txt

# cal-sync-magic is a private repo that build.sh stages into the build
# context. On a clean clone the [c] glob matches nothing, the
# requirements.txt anchor keeps COPY satisfied, and the calendar app is
# simply left out of the image (it's optional in settings too). Installed
# before the app source so code changes don't re-resolve its dependencies.
COPY requirements.txt cal-sync-magi[c] /opt/app/libs/cal-sync-magic/
RUN if [ -e /opt/app/libs/cal-sync-magic/setup.py ] \
      || [ -e /opt/app/libs/cal-sync-magic/setup.cfg ] \
      || [ -e /opt/app/libs/cal-sync-magic/pyproject.toml ]; then \
      pip install --no-cache-dir -e /opt/app/libs/cal-sync-magic; \
    else \
      echo "cal-sync-magic not in build context; calendar app disabled"; \
    fi

ENV GEOIP_PATH=/opt/app/geoip
COPY --from=geoip --chown=www-data:www-data /geoip /opt/app/geoip

# copy source (www-data owns it; thumbnails are generated into static/ at
# request time)
COPY --chown=www-data:www-data main /opt/app/main
COPY --chown=www-data:www-data static /opt/app/static
COPY --chown=www-data:www-data pigscanfly /opt/app/pigscanfly
COPY --chown=www-data:www-data templates /opt/app/templates
COPY --chown=www-data:www-data scripts/start-server.sh /opt/app/
COPY --chown=www-data:www-data *.py /opt/app/
WORKDIR /opt/app/

# start server
EXPOSE 80
STOPSIGNAL SIGTERM
CMD ["/opt/app/start-server.sh"]
