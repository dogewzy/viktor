FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ripgrep \
    nodejs \
    npm \
    openjdk-17-jdk-headless \
    && rm -rf /var/lib/apt/lists/*

# Coding Agent check_syntax: TypeScript/TSX/JSX 用 esbuild 做单文件语法检查
RUN npm install -g esbuild@0.25.5

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "main.py"]
