FROM python:3.11-slim

WORKDIR /app

# System deps for building any C-extension wheels not available prebuilt
# for this platform (e.g. some sqlglot/chromadb transitive deps).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first, separately from the source tree, so
# rebuilding after a source-only change reuses this layer.
COPY pyproject.toml README.md ./
COPY uada ./uada
RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "uada.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
