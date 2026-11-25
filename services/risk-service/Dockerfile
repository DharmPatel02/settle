FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1

WORKDIR /app
# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project
COPY src ./src
COPY models ./models
RUN uv sync --locked

RUN useradd --create-home --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8003
CMD ["uvicorn", "risk_service.main:app", "--host", "0.0.0.0", "--port", "8003"]
