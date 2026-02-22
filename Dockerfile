# SPDX-FileCopyrightText: 2025 Nobody (Generated)
#
# SPDX-License-Identifier: 0BSD

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set a working directory
WORKDIR /app

# Copy package files
COPY . /app/

# Install dependencies using uv from the lockfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && uv sync --frozen --no-dev \
    && apt-get remove -y git \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8080

# Default command runs the aiohttp webapp entrypoint
CMD ["flameeyes-paperless-web"]
