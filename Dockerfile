FROM python:3.12-slim

RUN pip install --upgrade pip --no-cache-dir

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt

COPY *.py /app/

CMD ["python", "/app/teslabuddy.py"]