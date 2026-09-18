# Pipeline image: server (Listener + Distributor + Agent + Deploy + API) and the
# edge node run from the same image — they are one repo and one dependency set,
# and compose picks the entrypoint per service.
#
# This is NOT the Verification Sandbox image. That one is deliberately minimal
# (server/sandbox/Dockerfile.sandbox); a candidate that only runs because the
# sandbox had the pipeline's dependencies installed is a false pass.
FROM python:3.13-slim

# The sandbox runner shells out to `docker` against the host daemon (the socket
# is bind-mounted by compose), so the CLI has to exist in this image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
CMD ["python", "-m", "server.main"]
