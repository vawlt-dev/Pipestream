# =============================================================================
# Agent Worker Dockerfile
# =============================================================================

FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY agent_worker.py .
COPY router.py .
COPY core.py .
COPY memory.py .
COPY research.py .
COPY schemas.py .
COPY tracing.py .
COPY tools_web.py .
COPY tools_google.py .
COPY workflows/ ./workflows/
COPY tests/ ./tests/
COPY pytest.ini .

# Create workspace directory
RUN mkdir -p /workspace

# Set environment defaults (override with -e or .env)
ENV WORK_DIR=/workspace
ENV OPENAI_API_BASE=http://host.docker.internal:1234/v1
ENV POLL_INTERVAL=10
ENV PYTHONUNBUFFERED=1

# Run the worker
CMD ["python", "agent_worker.py"]
