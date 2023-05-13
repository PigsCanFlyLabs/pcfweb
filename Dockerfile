FROM python:3.9-buster

# install nginx
RUN apt-get update && apt-get install nginx vim emacs libmariadbclient-dev default-libmysqlclient-dev libssl-dev -y
COPY /conf/nginx.default /etc/nginx/sites-available/default
RUN ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log
# install mysqlclient seperately because it's only in prod not dev.
RUN pip install mysqlclient

# copy source and install dependencies
RUN mkdir -p /opt/app
RUN mkdir -p /opt/app/libs/cal-sync-magic
COPY requirements.txt /opt/app/
RUN pip install --upgrade pip && pip install -r /opt/app/requirements.txt
RUN mkdir -p /opt/app/pip_cache
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
RUN chown -R www-data:www-data /opt/app
RUN cd /opt/app/libs/cal-sync-magic; pip install -e .

# start server
EXPOSE 80
STOPSIGNAL SIGTERM
CMD ["/opt/app/start-server.sh"]
