FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY karaqueue ./karaqueue
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime for YouTube these days; the deno extra brings one.
RUN pip install --no-cache-dir . "yt-dlp[default,deno]" && useradd --system --uid 1000 karaqueue && mkdir /data && chown karaqueue /data

USER karaqueue
ENV KARAQUEUE_DB=/data/karaqueue.db
VOLUME /data
EXPOSE 8000
CMD ["karaqueue"]
