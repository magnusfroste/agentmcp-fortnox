# agentmcp-fortnox: one company's Fortnox over MCP. Data, tokens and the write log live in /data.
FROM python:3.13.1-alpine
RUN apk add --no-cache poppler-utils
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8000
WORKDIR /app
COPY mcp_server.py index.py fortnox_api.py writes.py ./
RUN mkdir -p /data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:8000/health || exit 1
CMD ["python", "-u", "/app/mcp_server.py"]
