ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY worker.py /opt/dryrun-pilot/worker.py
RUN mkdir -p /workspace && chmod 755 /opt/dryrun-pilot /opt/dryrun-pilot/worker.py
USER 1000:1000
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-I", "/opt/dryrun-pilot/worker.py"]
