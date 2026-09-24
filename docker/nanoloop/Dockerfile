# nanoLoop as a Hagents runtime: one throwaway container per turn.
# The container is the sandbox (it plays the role OpenShell plays in nanoLoop's
# own run.sh): it sees only the mounts hagent computes for the agent.
#
#   docker build -t hagents-nanoloop:local docker/nanoloop
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG NANOLOOP_REF=main
RUN pip install --no-cache-dir "nanoloop @ git+https://github.com/ismaelfaro/nanoLoop@${NANOLOOP_REF}"
COPY patch_resolve.py /tmp/patch_resolve.py
RUN python /tmp/patch_resolve.py && rm /tmp/patch_resolve.py

RUN useradd -m -u 1000 agent
USER agent
# hagent mounts the agent's folder here and sets HARNESS_WORKDIR,
# NANOLOOP_MEMORY_DIR and NANOLOOP_SKILLS_DIR to live inside it.
WORKDIR /node
