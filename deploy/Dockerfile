FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir hatchling build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

# Betterleaks (maintained gitleaks fork): the external credential scanner. Fetched
# here so the runtime image stays slim; the corpus `leaks` module shells out to it.
FROM debian:12-slim AS leaks
ARG BETTERLEAKS_VERSION=1.8.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /tmp/bl.tar.gz \
      "https://github.com/betterleaks/betterleaks/releases/download/v${BETTERLEAKS_VERSION}/betterleaks_${BETTERLEAKS_VERSION}_linux_x64.tar.gz" \
 && tar -xzf /tmp/bl.tar.gz -C /tmp betterleaks \
 && install -m 0755 /tmp/betterleaks /usr/local/bin/betterleaks \
 && /usr/local/bin/betterleaks version

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
# Enable the external credential scanner; the leaks module falls back to its local
# regexes if the binary is ever absent.
ENV CORPUS_LEAKS_BIN=betterleaks
RUN useradd --create-home --uid 1000 corpus
COPY --from=leaks /usr/local/bin/betterleaks /usr/local/bin/betterleaks
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
USER corpus
ENTRYPOINT ["corpus"]
CMD ["api"]
