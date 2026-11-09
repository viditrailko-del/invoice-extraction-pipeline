FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.13.7

# Keep the venv OUTSIDE /app so the bind-mounted source (and any Windows .venv) can't clash.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (cached layer: only re-runs when deps change).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen

COPY . .

EXPOSE 8000
CMD ["uv", "run", "--frozen", "fastapi", "run", "app/main.py", "--host", "0.0.0.0", "--port", "8000"]
