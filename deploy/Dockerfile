# The application image. Deliberately boring.
#
# One stage, one process, no build step. The product is Python, Jinja templates
# and two hand-written static files; there is no bundler to run and nothing to
# compile, which is the same property that makes `make run` work on a reviewer's
# machine with nothing installed but Python.
#
# WHY THE DATABASE IS A VOLUME AND NOT IN THE IMAGE. SQLite is a file. Baking a
# seeded database into the layer would mean every redeploy silently discarded
# whatever the running instance had written -- an audit chain included, which is
# the one thing in this product that must never be lost. /data is mounted from
# the host and the entrypoint seeds it only when it is empty.
FROM python:3.12-slim

# No .pyc files to stale, and unbuffered so a log line appears when it happens
# rather than when the buffer fills.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STRATA_DB_PATH=/data/strata.db

WORKDIR /srv

# Requirements first, so a code change does not reinstall the world.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY scripts ./scripts

# Runs as nobody. The database directory is the only thing it needs to write,
# and it is a mount, so ownership is set at start rather than baked in.
RUN useradd --system --uid 10001 strata && mkdir -p /data && chown strata /data

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER strata
EXPOSE 8000

# The health check the proxy and `docker ps` both read. /healthz is deliberately
# outside the session guard; see app/web/deps.py.
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
