# Build-agent image for Jenkins: Python + C toolchain + static analysis.
# Pinning tools in an image means every build uses identical versions.
FROM python:3.12

RUN apt-get update \
 && apt-get install -y --no-install-recommends cppcheck \
 && rm -rf /var/lib/apt/lists/*
