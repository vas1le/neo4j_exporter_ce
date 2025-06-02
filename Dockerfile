FROM python:3.13-alpine

WORKDIR /app

# Copy requirements.txt for pip install
COPY requirements.txt /app/

RUN apk update && \
    apk add --no-cache --virtual .build-deps \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev && \
    apk add --no-cache \
    libffi \
    openssl \
    ca-certificates && \
    pip install --no-cache-dir -r requirements.txt && \
    apk del .build-deps && \
    rm -rf /var/cache/apk/*

# Copy only the contents of the host 'app/' folder into /app in the container
COPY app/ /app/

EXPOSE 8000

CMD ["python", "app.py"]
