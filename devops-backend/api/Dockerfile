# API runtime image with application dependencies and crane.
# Source is mounted from a ConfigMap; rebuild this image when dependencies change.
FROM python:3.10-slim

# Application dependencies.
RUN pip install --no-cache-dir --timeout 60 --retries 5 \
        fastapi uvicorn sqlalchemy psycopg2-binary kubernetes pyjwt \
        prometheus-client requests websockets python-multipart

# crane pushes user images to the cluster registry for POST /images.
ARG CRANE_VERSION=v0.20.2
RUN python -c "import urllib.request,tarfile,os; \
        urllib.request.urlretrieve('https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_Linux_x86_64.tar.gz','/tmp/cr.tgz'); \
        t=tarfile.open('/tmp/cr.tgz'); t.extract('crane','/usr/local/bin'); \
        os.chmod('/usr/local/bin/crane',0o755)" \
    && rm -f /tmp/cr.tgz \
    && crane version

WORKDIR /app
# main.py is mounted from the api-script ConfigMap at /app.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
