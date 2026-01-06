FROM ubuntu:latest

# Install system dependencies and build tools
RUN apt update && \
    apt upgrade -y && \
    apt install -y python3 python3-pip python3-venv curl

# Set application working directory
WORKDIR /app

# Create virtual environment
RUN python3 -m venv /app/venv

# Copy application source code
COPY . /app

# Upgrade pip and install dependencies in venv
RUN /app/venv/bin/pip install --upgrade pip && \
    /app/venv/bin/pip install -r requirements.txt

# Expose application port (adjust if needed)
EXPOSE 8000

# Run Gunicorn and Python scripts concurrently using venv
CMD ["bash", "-c", "/app/venv/bin/gunicorn app:app & /app/venv/bin/python main.py & /app/venv/bin/python ping.py"]
