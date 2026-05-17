FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml .
COPY node_agent/ node_agent/

RUN pip install --no-cache-dir .

ENV NODE_AGENT_CONFIG=/etc/node-agent/config.json
ENV LOG_LEVEL=INFO

VOLUME ["/run/press-node-agent"]
EXPOSE 8080

CMD ["node-agent"]
