"""Production web container: same-origin gateway to crud-api and rag-api.

Route table (src/frontend/web/README.md): catalog routes -> crud-api, chat and
/briefing* -> rag-api, /internal* and unrouted /api/* -> JSON 404, SPA
fallback for everything else.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
NGINX_CONF = ROOT / "src/frontend/web/nginx.conf"
PROXY_COMMON = ROOT / "src/frontend/web/proxy-common.inc"
COMPOSE = ROOT / "compose.yaml"
CRUD_UPSTREAM = "http://crud-api:8000"
RAG_UPSTREAM = "http://rag-api:8001"
CRUD_LOCATION = "~ ^/api/v1/(articles|article-facets|topics|locations|sources)(/|$)"
RAG_LOCATION = "~ ^/api/v1/(sessions|conversations|runs)(/|$)"
REQUIRED_DIRECTIVES = (
    "proxy_http_version 1.1;",
    "proxy_set_header Host $host;",
    "proxy_set_header X-Real-IP $remote_addr;",
    "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
    "proxy_set_header X-Forwarded-Proto $scheme;",
    "proxy_read_timeout 30s;",
)
PROXY_INCLUDE = "include /etc/nginx/conf.d/proxy-common.inc;"


def locations(conf: str) -> dict[str, str]:
    """Map each ``location <modifier> <path>`` header to its (flat) block body."""
    return {
        " ".join(match.group(1).split()): match.group(2)
        for match in re.finditer(r"location\s+([^{]+?)\s*\{([^}]*)\}", conf)
    }


def upstream_variables(conf: str) -> dict[str, str]:
    """``set $name value;`` declarations of the server block."""
    return dict(re.findall(r"set\s+(\$\w+)\s+([^;]+);", conf))


def test_shared_proxy_settings_cover_every_forwarding_directive() -> None:
    # Given
    common = PROXY_COMMON.read_text(encoding="utf-8")

    # Then
    for directive in REQUIRED_DIRECTIVES:
        assert directive in common, directive


def test_briefing_exact_and_prefix_locations_proxy_to_rag_api() -> None:
    # Given
    conf = NGINX_CONF.read_text(encoding="utf-8")
    blocks = locations(conf)
    variables = upstream_variables(conf)

    # When
    proxied = {key: blocks.get(key, "") for key in ("= /briefing", "^~ /briefing/")}

    # Then
    for key, body in proxied.items():
        assert "proxy_pass $rag_api;" in body, key
        assert PROXY_INCLUDE in body, key
        assert "try_files" not in body, key
    assert variables["$rag_api"] == RAG_UPSTREAM


def test_api_routes_go_to_their_owning_service() -> None:
    # Given
    conf = NGINX_CONF.read_text(encoding="utf-8")
    blocks = locations(conf)
    variables = upstream_variables(conf)

    # Then
    assert "proxy_pass $crud_api;" in blocks[CRUD_LOCATION]
    assert "proxy_pass $rag_api;" in blocks[RAG_LOCATION]
    for key in (CRUD_LOCATION, RAG_LOCATION):
        assert PROXY_INCLUDE in blocks[key], key
        assert "error_page 502 504 = @api_unavailable;" in blocks[key], key
    assert variables == {"$crud_api": CRUD_UPSTREAM, "$rag_api": RAG_UPSTREAM}
    # Upstream names are resolved per request (recreated containers change IP).
    assert re.search(r"resolver\s+127\.0\.0\.11\b", conf)


def test_proxy_pass_keeps_request_path_unchanged() -> None:
    # Given
    conf = NGINX_CONF.read_text(encoding="utf-8")

    # When
    upstreams = re.findall(r"^\s*proxy_pass\s+([^;]+);", conf, re.MULTILINE)

    # Then: only the upstream variables, whose values have no URI part after
    # host:port, so nginx forwards the original path and query verbatim.
    assert sorted(upstreams) == ["$crud_api", "$rag_api", "$rag_api", "$rag_api"]
    for value in upstream_variables(conf).values():
        assert re.fullmatch(r"http://[a-z-]+:\d+", value), value


def test_internal_and_unrouted_api_paths_answer_json_404() -> None:
    # Given
    blocks = locations(NGINX_CONF.read_text(encoding="utf-8"))

    # When
    internal = blocks["~ ^/internal(/|$)"]
    fallback = blocks["/api/"]

    # Then
    for body in (internal, fallback):
        assert "proxy_pass" not in body
        assert "default_type application/json;" in body
        assert re.search(r"return 404 '\{\"schema_version\":\"api_error\.v1\"", body)
    # A ^~ prefix would win over the API regex locations and hide every route.
    assert "^~ /api/" not in blocks


def test_spa_fallback_and_health_stay_local() -> None:
    # Given
    blocks = locations(NGINX_CONF.read_text(encoding="utf-8"))

    # When
    root_block = blocks.get("/", "")

    # Then
    assert "try_files $uri $uri/ /index.html;" in root_block
    assert "proxy_pass" not in root_block
    assert not any("health" in key for key in blocks)


def test_web_service_starts_after_both_apis() -> None:
    # Given
    compose = COMPOSE.read_text(encoding="utf-8")

    # When
    web = re.search(r"^  web:\n((?:    .*\n|\n)+?)(?=^  \S)", compose, re.MULTILINE)

    # Then
    assert web is not None
    depends = re.search(r"depends_on:\n((?:\s+- .+\n)+)", web.group(1))
    assert depends is not None
    assert {line.strip("- \n") for line in depends.group(1).splitlines()} == {
        "crud-api",
        "rag-api",
    }
