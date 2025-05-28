FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    python3-dev \
    libffi-dev \
    libsodium23 \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . .

# Install PDM
RUN pip install pdm

# Install project dependencies
RUN pdm install

# Create necessary directories
RUN mkdir -p /tmp/moonraker_dev/comms

# Expose port
EXPOSE 7125

# Start Moonraker
CMD ["pdm", "run", "python", "-m", "moonraker", "-c", "moonraker.conf", "-l", "debug"] 