FROM ghcr.io/astral-sh/uv:python3.13-alpine
LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid search add-on." \
      url="https://github.com/g0ldyy/comet"

RUN apk add --no-cache gcc python3-dev musl-dev linux-headers git make tzdata mimalloc2

WORKDIR /app

COPY pyproject.toml uv.lock ./

ENV TZ=UTC \
    UV_HTTP_TIMEOUT=300 \
    PYTHONMALLOC=malloc \
    LD_PRELOAD=/usr/lib/libmimalloc.so.2
ARG TARGETPLATFORM
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETPLATFORM},sharing=locked uv sync --frozen

RUN set -eu; mkdir -p /app/data; y=$(date +%Y); m=$(date +%m); m=${m#0}; ok=0; \
    for i in 0 1 2 3; do \
      mm=$((m - i)); yy=$y; while [ $mm -le 0 ]; do mm=$((mm + 12)); yy=$((yy - 1)); done; \
      ym=$(printf '%04d-%02d' $yy $mm); \
      if wget -qO /tmp/geo.gz "https://download.db-ip.com/free/dbip-country-lite-${ym}.mmdb.gz"; then \
        gunzip -c /tmp/geo.gz > /app/data/dbip-country-lite.mmdb; rm /tmp/geo.gz; ok=1; break; \
      fi; \
    done; \
    [ $ok -eq 1 ]

COPY . .

ARG DATABASE_PATH
ARG COMET_COMMIT_HASH
ARG COMET_BUILD_DATE
ARG COMET_BRANCH

ENV COMET_COMMIT_HASH=${COMET_COMMIT_HASH} \
    COMET_BUILD_DATE=${COMET_BUILD_DATE} \
    COMET_BRANCH=${COMET_BRANCH}

ENTRYPOINT ["uv", "run", "python", "-m", "comet.main"]
