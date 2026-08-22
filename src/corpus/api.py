"""FastAPI REST surface."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from . import __version__, search

app = FastAPI(title="corpus", version=__version__)


class SearchRequest(BaseModel):
    query: str
    top_k: int = 10
    filters: dict[str, Any] | None = None


class QueryRequest(BaseModel):
    label: str | None = None
    account: str | None = None
    before: str | None = None
    after: str | None = None
    limit: int = 500


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/search")
def search_endpoint(req: SearchRequest) -> dict[str, Any]:
    return {"results": search.semantic_search(req.query, req.top_k, req.filters)}


@app.post("/query")
def query_endpoint(req: QueryRequest) -> dict[str, Any]:
    results = search.structured_query(
        label=req.label,
        account=req.account,
        before=req.before,
        after=req.after,
        limit=req.limit,
    )
    return {"count": len(results), "results": results}


@app.get("/stats")
def stats_endpoint() -> dict[str, Any]:
    return search.stats()
