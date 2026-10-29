# image-watch. "base" has Python and the Anthropic SDK; "claude-code" adds the
# Claude Code CLI (Node) for summaries on a Claude subscription.
# install.sh picks the target (AI_TARGET in .env).
FROM python:3.13-slim-bookworm AS base
RUN pip install --no-cache-dir "anthropic>=1,<2"
WORKDIR /app
USER 1000:1000

FROM base AS claude-code
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /root/.npm
USER 1000:1000
