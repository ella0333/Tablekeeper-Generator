FROM python:3.12-slim

# Pure standard library service: no third-party packages and no build-time
# network access are required. The base image already ships the IANA timezone
# database at /usr/share/zoneinfo, which zoneinfo uses for DST handling.

WORKDIR /app
COPY app.py /app/app.py

ENV PORT=8080
EXPOSE 8080

CMD ["python", "-u", "/app/app.py"]