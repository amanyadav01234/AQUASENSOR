FROM python:3.12-slim
WORKDIR /app
COPY server.py device_client.py ./
COPY web ./web
COPY data ./data
ENV PORT=8000 AQUA_DB=/data/aquasense.db
VOLUME /data
EXPOSE 8000
# Set AQUA_SECRET at runtime:  docker run -e AQUA_SECRET=... -p 8000:8000 -v aqua:/data aquasense
CMD ["python", "server.py"]
