# agentmcp-fortnox: Fortnox over MCP, for one company or several. Data, tokens and the write logs live in /data.
FROM python:3.13.1-alpine
RUN apk add --no-cache poppler-utils su-exec \
 && addgroup -S -g 10001 fortnox && adduser -S -D -H -u 10001 -G fortnox fortnox
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8000
WORKDIR /app
COPY mcp_server.py index.py fortnox_api.py writes.py entrypoint.sh ./
RUN mkdir -p /data && chown fortnox:fortnox /data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:8000/health || exit 1
# The entrypoint starts as root only to give /data to the user "fortnox", then runs the service as it.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-u", "/app/mcp_server.py"]
