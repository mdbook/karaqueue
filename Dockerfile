FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY karaqueue ./karaqueue
COPY requirements.lock ./requirements.lock
COPY ci ./ci
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime for YouTube these days; the deno extra brings one.
RUN python ci/lock.py check && pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --no-deps --no-build-isolation . \
    && pip check \
    && useradd --system --uid 1000 karaqueue && mkdir /data && chown karaqueue /data

USER karaqueue
ARG EXTRACTOR_SMOKE=0
RUN if [ "$EXTRACTOR_SMOKE" = 1 ]; then python ci/smoke.py --fetch; else python ci/smoke.py; fi
ENV KARAQUEUE_DB=/data/karaqueue.db
VOLUME /data
EXPOSE 8000
CMD ["karaqueue"]
