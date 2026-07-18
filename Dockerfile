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

CMD ["python", "/app/teslabuddy.py"]