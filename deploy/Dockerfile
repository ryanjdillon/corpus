# Base images are pinned by digest, so a rebuild of a
# release gets the same layers; the tag stays in the reference for readers and
# Dependabot (docker ecosystem), which proposes the bumps.
FROM python:3.15-slim@sha256:2da676046630d7bf14c6764ae1143dc7fa0377a53f94bf5a642f0b214aca5bce AS build
WORKDIR /src
RUN pip install --no-cache-dir hatchling build
COPY pyproject.toml README.md ./
COPY src ./src
COPY packages/detect ./packages/detect
# Two wheels: corpus and the corpus-detect it depends on. Installed together below,
# so the dependency resolves to this build's wheel, never to an index.
RUN python -m build --wheel --outdir /dist packages/detect \
 && python -m build --wheel --outdir /dist

# Betterleaks (maintained gitleaks fork): the external credential scanner. Fetched
# here so the runtime image stays slim; corpus_detect's `leaks` module shells out to it.
# It reads the whole archive in the sensitive tier, so the tarball is checked against
# a pinned sha256 before it is unpacked: a tampered or swapped release fails the
# build. The pin is the release's cosign-signed checksums.txt line, verified when it
# was bumped (docs/development.md#build); `just verify-dockerfile-checksum` checks
# that a wrong pin fails.
FROM debian:12-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS leaks
ARG BETTERLEAKS_VERSION=1.8.1
ARG BETTERLEAKS_SHA256=efa407244e1ea8e35f582b8a42becdeac08bdead04f68eb752adda722d583c2a
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /tmp/bl.tar.gz \
      "https://github.com/betterleaks/betterleaks/releases/download/v${BETTERLEAKS_VERSION}/betterleaks_${BETTERLEAKS_VERSION}_linux_x64.tar.gz" \
 && echo "${BETTERLEAKS_SHA256}  /tmp/bl.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/bl.tar.gz -C /tmp betterleaks \
 && install -m 0755 /tmp/betterleaks /usr/local/bin/betterleaks \
 && /usr/local/bin/betterleaks version

FROM python:3.15-slim@sha256:2da676046630d7bf14c6764ae1143dc7fa0377a53f94bf5a642f0b214aca5bce
ENV PYTHONUNBUFFERED=1
# Enable the external credential scanner. corpus_detect.leaks falls back to its local
# regexes if the binary is ever absent or fails; the enrich audit counts which ran
# (corpus.detect.leaks_scanner), so a fallback is visible.
ENV CORPUS_LEAKS_BIN=betterleaks
RUN useradd --create-home --uid 1000 corpus
COPY --from=leaks /usr/local/bin/betterleaks /usr/local/bin/betterleaks
COPY --from=build /dist/*.whl /tmp/
# Install against the pinned constraints, so a rebuild of the same release gets the
# same dependency set (pyproject.toml only states ranges). `just lock` regenerates it.
COPY deploy/constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -c /tmp/constraints.txt /tmp/*.whl \
 && rm -f /tmp/*.whl /tmp/constraints.txt
USER corpus
ENTRYPOINT ["corpus"]
CMD ["serve", "sensitive"]
