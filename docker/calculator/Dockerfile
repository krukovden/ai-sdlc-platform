FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg python3 git ca-certificates apt-transport-https lsb-release \
    && curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor >/usr/share/keyrings/microsoft.gpg \
    && chmod go+r /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ $(lsb_release -cs) main" \
        >/etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends azure-cli \
    && rm -rf /var/lib/apt/lists/* \
    && npm i -g @github/copilot

RUN useradd --create-home --shell /bin/bash app

RUN git clone --depth 1 https://github.com/krukovden/engineering-catalog /opt/engineering-catalog \
    && az extension add --name azure-devops --yes

WORKDIR /work/platform
COPY --chown=app:app . /work/platform

USER app

RUN mkdir -p /home/app/.copilot/skills \
    && ln -s /opt/engineering-catalog/skills/shared/ado-board /home/app/.copilot/skills/ado-board

CMD ["python3", "docker/calculator/scenario.py"]
