FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY apps ./apps
COPY config ./config
COPY core ./core
COPY database ./database
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY alembic ./alembic
# Official IBKR ibapi is intentionally not fetched from an unofficial PyPI package.
# Install the wheel/source obtained from the official TWS API download before enabling broker mode.
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
