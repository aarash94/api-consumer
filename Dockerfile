FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 app
USER 10001

ENTRYPOINT ["api-consumer"]
CMD ["--help"]
