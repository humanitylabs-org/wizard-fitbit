FROM python:3.12-alpine
WORKDIR /app
COPY app /app/app
COPY web /app/web
COPY openapi.json /app/openapi.json
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
ENV DATA_DIR=/data/private PORT=8830
EXPOSE 8830
CMD ["python", "/app/app/server.py"]
