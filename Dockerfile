FROM python:3.12-slim

# 镜像源可通过 --build-arg 覆盖（默认清华源，国内构建更稳）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    UV_LINK_MODE=copy

WORKDIR /app

# 安装 uv（走镜像）
RUN pip install --no-cache-dir --index-url ${PIP_INDEX_URL} uv

# 先装依赖（利用缓存层，代码变更不触发依赖重装）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8765

CMD ["python", "run.py"]
