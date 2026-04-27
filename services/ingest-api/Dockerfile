# Build context: services/  (so _shared/metrics.py is reachable).
FROM python:3.11-slim
WORKDIR /app
COPY ingest-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY _shared/metrics.py /app/metrics.py
COPY ingest-api/app.py .
EXPOSE 8004
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8004"]
