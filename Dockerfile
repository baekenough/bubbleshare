FROM node:22-alpine AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.9.26 AS uv
FROM python:3.12-slim-bookworm

COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 자주 바뀌는 앱 코드를 복사하기 전에 잠금 파일의 의존성을 설치한다.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY VIVI_MVP_Sample_Dataset/ ./VIVI_MVP_Sample_Dataset/
COPY evals/ ./evals/
COPY --from=frontend /ui/dist /app/frontend/dist
RUN uv sync --frozen --no-dev \
    && mkdir -p /app/.vivi /app/output

ENTRYPOINT ["vivi-agent"]
CMD ["--help"]
