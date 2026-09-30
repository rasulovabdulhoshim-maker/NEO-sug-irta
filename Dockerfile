FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Subtitr modelini oldindan yuklab qo'yamiz (birinchi so'rov sekin bo'lmasin)
ARG WHISPER_MODEL=small
ENV WHISPER_MODEL=${WHISPER_MODEL}
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

# Kodni yig'amiz. Ikki xil joylashuv qo'llanadi:
#  1) app/ papkasi bilan (asl tuzilma)
#  2) hamma fayl bitta joyda (GitHub'ga papkasiz yuklangan bo'lsa)
COPY . /tmp/src
RUN mkdir -p /srv/app/static \
 && if [ -f /tmp/src/app/main.py ]; then \
      cp -r /tmp/src/app/. /srv/app/; \
    else \
      cp /tmp/src/__init__.py /tmp/src/main.py /tmp/src/ai.py /tmp/src/editing.py /srv/app/ \
      && cp /tmp/src/index.html /srv/app/static/index.html; \
    fi \
 && rm -rf /tmp/src

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
