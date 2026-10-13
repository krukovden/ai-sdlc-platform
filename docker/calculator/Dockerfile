FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm i -g @github/copilot

RUN useradd --create-home --shell /bin/bash app

WORKDIR /work/platform
COPY --chown=app:app . /work/platform

USER app

CMD ["python3", "docker/calculator/scenario.py"]
