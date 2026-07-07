FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1

# install nginx
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
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
