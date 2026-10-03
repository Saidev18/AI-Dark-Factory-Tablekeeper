FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py /app/server.py
ENV PORT=8080 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
EXPOSE 8080
CMD ["python", "/app/server.py"]
