# Build only from an operator-reviewed validation/ directory.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY check.py probe.py /opt/validator/
USER 65534:65534
ENTRYPOINT ["python", "-I", "-B", "/opt/validator/check.py"]
