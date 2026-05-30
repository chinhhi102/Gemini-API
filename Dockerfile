FROM python:3.12-slim

WORKDIR /app

# setuptools_scm derives the version from git, which is absent in the image.
# Pin it so the editable install of the Gemini-API package succeeds.
ARG SETUPTOOLS_SCM_PRETEND_VERSION=1.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION}

# Build metadata for setuptools_scm; copy minimal files first for layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY service ./service

# Install the Gemini-API library plus the service's web dependencies.
RUN pip install --no-cache-dir -e . \
    && pip install --no-cache-dir -r service/requirements.txt

# Writable location for accounts.json and auto-refreshed cookies.
ENV DATA_DIR=/data \
    GEMINI_COOKIE_PATH=/data/cookies
RUN mkdir -p /data/cookies

EXPOSE 8000

CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000"]
