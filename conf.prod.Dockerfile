FROM python:3.12-bookworm

# 设置工作目录
RUN mkdir /code
WORKDIR /code

# 设置环境变量（不经常变化）
ENV RUN_MODE=aliyun-prod
ENV PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 配置系统源和安装系统依赖（不经常变化）
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources
RUN apt update
# debian11 以及下
# RUN apt install -y vim netcat telnet less
# debian12 的  netcat 变成 netcat-openbsd 了，详见: https://unix.stackexchange.com/questions/749306/discrepancies-in-netcat-installation-process-between-debian-bullseye-vs-debian-b
RUN apt install -y vim netcat-openbsd telnet less iputils-ping
RUN apt install -y netcat-openbsd htop sysstat
RUN apt install -y ncdu
RUN apt install -y libjemalloc-dev
RUN apt-get update && apt-get install --no-install-recommends -y --force-yes zlib1g-dev time libffi-dev build-essential libssl-dev libbz2-dev liblzma-dev vim net-tools
RUN apt install -y ffmpeg
# Coding Agent check_syntax: JavaScript 使用 node --check，Java 使用 javac，
# TypeScript/TSX/JSX 使用 esbuild（纯解析单文件语法检查，不需要 node_modules/tsconfig）。
RUN apt-get update && apt-get install --no-install-recommends -y nodejs npm openjdk-17-jdk-headless
RUN npm install -g --registry=https://registry.npmmirror.com esbuild@0.25.5

# 升级pip并安装Python依赖（不经常变化）
RUN python -m pip install --upgrade pip -i $PYPI_INDEX_URL

# 先复制requirements文件（不经常变化）
COPY requirements.txt /code/

# 安装Python依赖
RUN pip install -r requirements.txt -i $PYPI_INDEX_URL

# 最后复制应用代码（经常变化，放在最后）
ADD . /code/
