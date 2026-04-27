# Build context: services/  (so _shared/metrics.py is reachable).
# Build with: docker build -f search-api/Dockerfile services/
FROM python:3.11-slim
WORKDIR /app
COPY search-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY _shared/metrics.py /app/metrics.py
COPY search-api/app.py .
EXPOSE 8010
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]
