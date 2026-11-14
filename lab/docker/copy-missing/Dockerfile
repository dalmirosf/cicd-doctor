# Case docker-copy: requirements-missing.txt is not in the build context.
FROM python:3.13-slim
WORKDIR /app
COPY requirements-missing.txt /app/
RUN pip install -r requirements-missing.txt
