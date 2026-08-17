# Local OSS API image. No secrets — never COPY .env (see .dockerignore).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY infona_client ./infona_client
RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["uvicorn", "infona_client.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
