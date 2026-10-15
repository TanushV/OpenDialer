FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /usr/local/bin/uv
WORKDIR /opt/opendialer
COPY pyproject.toml uv.lock ./
COPY services/backend services/backend
RUN uv sync --frozen --no-dev --no-editable
COPY services/backend/migrations migrations
COPY services/backend/alembic.ini alembic.ini
FROM python:3.12-slim
RUN groupadd --gid 10001 dialer && useradd --uid 10001 --gid 10001 --create-home dialer
RUN mkdir -p /var/lib/opendialer && chown dialer:dialer /var/lib/opendialer
WORKDIR /opt/opendialer
COPY --from=build /opt/opendialer /opt/opendialer
ENV PATH="/opt/opendialer/.venv/bin:$PATH" PYTHONPATH=/opt/opendialer/services/backend OPENDIALER_DATA_DIR=/var/lib/opendialer OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 PYTHONUNBUFFERED=1
USER dialer
EXPOSE 8765
ENTRYPOINT ["python", "-m", "app.cli"]
CMD ["serve", "--mode", "connected", "--host", "0.0.0.0"]
