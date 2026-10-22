# 统一运行镜像：已包含语言运行时和基础命令，避免评分阶段联网安装。
FROM adminfather/benzhi-claude-code2:20260919@sha256:6bc4ad09b1c450579b25e61e7c797328ebaf4ac4eb086c078d2ec1e9f7742046

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 覆盖基础镜像的交互式入口；解题与参考解身份由 task.toml 指定。
ENTRYPOINT []
USER root
RUN if [ ! -e /usr/local/bin/python3 ]; then ln -s /usr/bin/python3 /usr/local/bin/python3; fi \
    && useradd --create-home --uid 1001 runner

WORKDIR /app
COPY app/ /app/
RUN chown -R runner:runner /app
