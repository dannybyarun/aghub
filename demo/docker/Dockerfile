FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt/aghub/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "PyYAML>=6.0"

WORKDIR /opt/aghub

COPY src/ /opt/aghub/src/
COPY bin/ /opt/aghub/bin/
COPY demo/docker/ /opt/aghub/demo/docker/

RUN chmod +x /opt/aghub/demo/docker/*.sh

CMD ["python", "-m", "aghub", "--help"]
