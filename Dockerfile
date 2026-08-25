FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip --no-cache-dir

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt

COPY *.py /app/

# teslabuddy refreshes /tmp/teslabuddy.healthy while it is connected to MQTT
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,sys,time; p=os.environ.get('HEALTH_FILE','/tmp/teslabuddy.healthy'); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 120 else 1)"

CMD ["python", "/app/teslabuddy.py"]