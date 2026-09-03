# Use official lightweight Python image
FROM python:3.12-slim

LABEL maintainer="BlitzPack Contributors" \
      description="Intelligent, size-tiered parallel compression engine producing seekable .blitz archives"

# Set environment variables for containerized Python
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Set application directory
WORKDIR /app

# Copy dependency specifications first to leverage Docker layer caching
COPY pyproject.toml requirements.txt ./

# Install core dependencies and rich CLI
RUN pip install --upgrade pip && \
    pip install .[cli]

# Copy application source files
COPY blitzpack/ ./blitzpack/
COPY cli.py gui.py ./

# Install BlitzPack CLI entry points
RUN pip install --no-deps -e .

# Set default directory for user data mounts
WORKDIR /data

# Default executable entrypoint
ENTRYPOINT ["blitzpack"]

# Default argument displays help
CMD ["--help"]
