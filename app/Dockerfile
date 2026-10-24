FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /app/requirements.lock
COPY app /app/app
COPY web /app/web
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
ENV DATA_DIR=/data PORT=18812 PYTHONDONTWRITEBYTECODE=1
EXPOSE 18812
CMD ["python", "/app/app/server.py"]
