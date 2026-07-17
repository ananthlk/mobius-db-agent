# mobius-db-agent — governed DB access MCP server + org doc-store provisioner.
# Binds 0.0.0.0 and honors Cloud Run's PORT (app/server.py); DB access via the
# Cloud SQL unix socket (--add-cloudsql-instances) with PGPASSWORD from Secret
# Manager — same pattern as mobius-chat.
FROM python:3.12-slim

WORKDIR /srv/mobius-db-agent

# libpq for psycopg2 (binary wheel usually suffices, but keep parity with fleet images)
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY manifests ./manifests
COPY org_docs_schema ./org_docs_schema
COPY db_client.py .

CMD ["python", "-m", "app"]
