# Python 3.11 slim，按镜像索引 digest 固定；题目仅使用标准库。
FROM python:3.11-slim@sha256:0dd364ba7e10242f07755449e3a3d0e35f9efd987952737b90def6709ab0c5ce

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 解题与参考解身份由 task.toml 的 [agent] user = "runner" 指定。
RUN useradd --create-home --uid 1000 runner

WORKDIR /app
COPY app/ /app/
RUN chown -R runner:runner /app
