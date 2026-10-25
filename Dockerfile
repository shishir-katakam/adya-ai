FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY harness.py server.py ./
COPY adya-share ./adya-share
COPY static ./static
COPY docs ./docs

EXPOSE 8080
CMD ["python", "server.py"]
