# SPDX-FileCopyrightText: 2025 Nobody (Generated)
#
# SPDX-License-Identifier: 0BSD

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# Set a working directory
WORKDIR /app

# Copy package files
COPY . /app/

# Install pip and build essentials for any optional compilation
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && pip install --upgrade pip setuptools wheel \
    # Install pdfrename from the GitHub repo (main branch). This keeps the image
    # self-contained even if the user doesn't provide the pdfrename package.
    && pip install "git+https://github.com/Flameeyes/pdfrename.git@main" \
    && pip install -e . \
    && apt-get remove -y build-essential git \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8080

# Default command runs the aiohttp webapp entrypoint
CMD ["flameeyes-paperless-web"]
