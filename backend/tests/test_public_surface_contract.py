from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter
from fastapi.routing import APIRoute

from windops_backend.api import router as business_router
from windops_backend.knowledge_graph.api import router as knowledge_graph_router

EXPECTED_API_ROUTE_COUNT = 126
EXPECTED_API_ROUTE_SHA256 = "8402a8826c8e4ebe1a76f895178f3e6e5801cac5a379af3729e163b27e30bfdf"
LEGACY_API_ROUTE_SHA256 = "653247fa7cada71548edeccba02bd0c04f68f03fad14261613338db3ee68790d"


def _route_signature(router: APIRouter) -> list[dict[str, Any]]:
    return [
        {
            "path": f"/api/v1{route.path}",
            "methods": sorted(route.methods or ()),
            "name": route.name,
        }
        for route in router.routes
        if isinstance(route, APIRoute)
    ]


def test_fastapi_public_route_surface_matches_the_recorded_baseline() -> None:
    routes = [
        *_route_signature(business_router),
        *_route_signature(knowledge_graph_router),
    ]
    canonical = json.dumps(
        sorted(routes, key=lambda row: (row["path"], row["methods"], row["name"])),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert len(routes) == EXPECTED_API_ROUTE_COUNT
    assert hashlib.sha256(canonical).hexdigest() == EXPECTED_API_ROUTE_SHA256
    from windops_backend.api.engineering_claims import router as claim_router
    from windops_backend.api.knowledge_passages import router as passage_router
    from windops_backend.api.knowledge_review import router as review_router
    from windops_backend.api.structural import router as structural_router

    assert len(_route_signature(structural_router)) == 13
    assert len(_route_signature(passage_router)) == 3
    assert len(_route_signature(review_router)) == 3
    from windops_backend.api.structural_workflow import router as workflow_router

    assert len(_route_signature(workflow_router)) == 8
    assert len(_route_signature(claim_router)) == 3
    parser_capabilities = [
        row for row in routes if row["path"] == "/api/v1/knowledge/parser-capabilities"
    ]
    assert parser_capabilities == [
        {
            "path": "/api/v1/knowledge/parser-capabilities",
            "methods": ["GET"],
            "name": "knowledge_parser_capabilities",
        }
    ]
    additions = [
        *_route_signature(structural_router),
        *_route_signature(passage_router),
        *_route_signature(review_router),
        *_route_signature(claim_router),
        *_route_signature(workflow_router),
        *parser_capabilities,
    ]
    assert len(additions) == 31
    legacy = [row for row in routes if row not in additions]
    assert len(legacy) == 95
    legacy_canonical = json.dumps(
        sorted(legacy, key=lambda row: (row["path"], row["methods"], row["name"])),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert hashlib.sha256(legacy_canonical).hexdigest() == LEGACY_API_ROUTE_SHA256
