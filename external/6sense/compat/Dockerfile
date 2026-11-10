# syntax=docker/dockerfile:1

# T7 compatibility track: the two Go components that implement 6Sense's
# dealiasing gate. The LSTM generator is intentionally not included; see
# README.md for the validity boundary.
FROM golang:1.23-bookworm AS build

ARG SCANV6_COMMIT=67f6ceca2318017d0a96361056e527020c15b1b0
ARG ALIASV6_COMMIT=e357120ba138798867059af992647ddddebda39e
ARG GOPROXY=https://goproxy.cn,direct

ENV GOPROXY=${GOPROXY}
ENV PATH=/usr/local/go/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends git libpcap-dev \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    for attempt in 1 2 3 4 5; do \
      git clone --quiet https://github.com/IPv6-Security/scanv6 /src/scanv6 && break; \
      sleep 2; \
    done; \
    test -d /src/scanv6; \
    git -C /src/scanv6 checkout --quiet "${SCANV6_COMMIT}"; \
    test "$(git -C /src/scanv6 rev-parse HEAD)" = "${SCANV6_COMMIT}"

RUN set -eux; \
    for attempt in 1 2 3 4 5; do \
      git clone --quiet https://github.com/IPv6-Security/offline-dealiaser /src/aliasv6 && break; \
      sleep 2; \
    done; \
    test -d /src/aliasv6; \
    git -C /src/aliasv6 checkout --quiet "${ALIASV6_COMMIT}"; \
    test "$(git -C /src/aliasv6 rev-parse HEAD)" = "${ALIASV6_COMMIT}"

RUN cd /src/scanv6 && go mod download && \
    go build -o /usr/local/bin/scanv6 ./cmd/scanv6
RUN cd /src/aliasv6 && go mod download && \
    go build -o /usr/local/bin/aliasv6 ./cmd/aliasv6

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpcap0.8 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/bin/scanv6 /usr/local/bin/scanv6
COPY --from=build /usr/local/bin/aliasv6 /usr/local/bin/aliasv6
COPY compat_runner.py /usr/local/lib/6sense-compat/compat_runner.py

ENTRYPOINT ["python", "/usr/local/lib/6sense-compat/compat_runner.py"]
