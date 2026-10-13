FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    AZURE_EXTENSION_DIR=/opt/azure-cli/extensions

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg python3 git ca-certificates apt-transport-https lsb-release sudo \
    && curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor >/usr/share/keyrings/microsoft.gpg \
    && chmod go+r /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ $(lsb_release -cs) main" \
        >/etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends azure-cli \
    && rm -rf /var/lib/apt/lists/* \
    && npm i -g @github/copilot

RUN groupadd --system idpwork \
    && useradd --create-home --shell /bin/bash --gid idpwork idp \
    && useradd --create-home --shell /bin/bash --gid idpwork agent

RUN git clone --depth 1 https://github.com/krukovden/engineering-catalog /opt/engineering-catalog \
    && mkdir -p "$AZURE_EXTENSION_DIR" \
    && az extension add --name azure-devops --yes \
    && chmod -R a+rX "$AZURE_EXTENSION_DIR" \
    && printf 'idp ALL=(root) NOPASSWD: /usr/sbin/runuser -u agent -- *\n' >/etc/sudoers.d/idp-runuser-agent \
    && chmod 0440 /etc/sudoers.d/idp-runuser-agent

WORKDIR /work/platform
COPY --chown=idp:idpwork . /work/platform

RUN mkdir -p /home/idp/.copilot/skills \
    && ln -s /opt/engineering-catalog/skills/shared/ado-board /home/idp/.copilot/skills/ado-board \
    && chown -R idp:idpwork /work/platform /home/idp /home/agent \
    && chmod -R g+rwX /work/platform /home/idp /home/agent

COPY docker/calculator/entrypoint.sh /usr/local/bin/idp-entrypoint.sh
RUN chmod 0755 /usr/local/bin/idp-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/idp-entrypoint.sh"]
CMD ["python3", "docker/calculator/scenario.py"]
