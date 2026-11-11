# Existing standalone Python runtime, verified locally; build with --pull=false.
ARG BASE_IMAGE=python@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134
FROM ${BASE_IMAGE}
USER root
WORKDIR /observer
COPY exporter.py /observer/exporter.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data PYTHONPATH=""
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/python", "/observer/exporter.py"]
CMD []
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD ["/usr/local/bin/python", "-B", "-c", "import socket; s=socket.create_connection(('127.0.0.1',9116),timeout=2); s.sendall(b'GET /health HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n'); r=s.recv(128); s.close(); assert b' 200 ' in r.split(b'\\r\\n',1)[0]"]
