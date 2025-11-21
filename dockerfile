FROM python:3.10-slim

# Keeps Python from generating .pyc files in the container
ENV PYTHONDONTWRITEBYTECODE=1
# Turns off buffering for easier container logging
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install supervisor for running multiple services and curl for healthcheck
RUN apt-get update && apt-get install -y supervisor curl && rm -rf /var/lib/apt/lists/*

# Create log directories
RUN mkdir -p /var/log/supervisor

RUN mkdir -p /app/src
COPY ./requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade -r requirements.txt
COPY ./src/ /app/src
COPY ./supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Expose both ports: 5032 for OAuth callback (public), 5033 for dashboard (internal)
EXPOSE 5032 5033

# Run as root (supervisor requires it)
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
