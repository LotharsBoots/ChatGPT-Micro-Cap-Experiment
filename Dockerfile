FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Minimal OS deps for wheels + certs
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps + AWS CLI for s3 sync in task commands
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt awscli

# Copy code
COPY . .

# Default is inert; EventBridge overrides at runtime with the real command
CMD ["python","-V"]



