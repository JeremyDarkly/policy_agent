FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables for development
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy requirements file from backend directory
COPY app/backend/requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend application code
COPY app/backend ./app/backend

# Expose port
EXPOSE 8000

# Run the backend server (uvicorn, no auto-reload — restart the container to
# pick up backend code changes; the source is bind-mounted in docker-compose)
CMD ["python", "app/backend/server.py"]
