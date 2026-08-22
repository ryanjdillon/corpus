FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir hatchling build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 1000 corpus
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
USER corpus
ENTRYPOINT ["corpus"]
CMD ["api"]
