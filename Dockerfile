FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FISHING_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev portaudio19-dev libportaudio2 \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc libc6-dev portaudio19-dev \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 fishing \
    && mkdir /data \
    && chown fishing:fishing /data

COPY configuration.py logger.py main.py normal_distribution.py servo.py webui.py ./
COPY web/ ./web/
COPY tests/ ./tests/

USER fishing
STOPSIGNAL SIGTERM
CMD ["python", "webui.py"]
