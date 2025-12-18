# Dockerfile for CV Job Informer
# Tracks Kubernetes jobs and reports to CloudVision

FROM python:3.11-slim

# Metadata
LABEL maintainer="Arista Networks"
LABEL description="CV Job Informer - Tracks Kubernetes jobs and reports to CloudVision"
LABEL version="1.0.0"
LABEL org.opencontainers.image.title="CV Job Informer"
LABEL org.opencontainers.image.description="Kubernetes job monitoring service for CloudVision integration"
LABEL org.opencontainers.image.vendor="Arista Networks"
LABEL org.opencontainers.image.version="1.0.0"
LABEL org.opencontainers.image.source="https://github.com/aristanetworks/cloudvision-job-kubernetes"

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && \
  apt-get install -y --no-install-recommends \
  ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Install Python dependencies directly
RUN pip install --no-cache-dir \
  kubernetes>=28.0.0 \
  requests>=2.31.0 \
  urllib3>=2.0.0

# Copy application files
COPY main.py .
COPY job_monitor.py .
COPY node_monitor.py .
COPY models.py .
COPY constants.py .
COPY pod_handler.py .
COPY job_handler.py .
COPY api_utils.py .
COPY interface_discovery.py .

# Create directory for logs
RUN mkdir -p /logs

# Set Python to run in unbuffered mode for real-time logs
ENV PYTHONUNBUFFERED=1

# Run as non-root user for security
RUN useradd -m -u 1000 jobinformer && \
  chown -R jobinformer:jobinformer /app /logs
USER jobinformer

# Health check (optional - checks if process is running)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD pgrep -f main.py || exit 1

# Default command - can be overridden in deployment
ENTRYPOINT ["python3", "main.py"]

# Default arguments - should be overridden in deployment
CMD ["--namespace", "default", "--log-level", "INFO"]

