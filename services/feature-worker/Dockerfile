# Build context: services/  (so _shared/metrics.py is reachable).
FROM python:3.11-slim

WORKDIR /app
COPY feature-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY _shared/metrics.py /app/metrics.py
COPY feature-worker/worker.py .

EXPOSE 9100
CMD ["python", "-u", "worker.py"]
