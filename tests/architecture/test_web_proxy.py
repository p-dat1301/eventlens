"""Production web container must forward same-origin /briefing* calls to rag-api."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
NGINX_CONF = ROOT / "src/frontend/web/nginx.conf"
COMPOSE = ROOT / "compose.yaml"
RAG_UPSTREAM = "http://rag-api:8001"
REQUIRED_DIRECTIVES = (
    "proxy_http_version 1.1;",
    "proxy_set_header Host $host;",
    "proxy_set_header X-Real-IP $remote_addr;",
    "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
    "proxy_set_header X-Forwarded-Proto $scheme;",
    "proxy_read_timeout 30s;",
)


def locations(conf: str) -> dict[str, str]:
    """Map each ``location <modifier> <path>`` header to its (flat) block body."""
    return {
        " ".join(match.group(1).split()): match.group(2)
        for match in re.finditer(r"location\s+([^{]+?)\s*\{([^}]*)\}", conf)
    }


def test_briefing_exact_and_prefix_locations_proxy_to_rag_api() -> None:
    # Given
    blocks = locations(NGINX_CONF.read_text(encoding="utf-8"))

    # When
    proxied = {key: blocks.get(key, "") for key in ("= /briefing", "^~ /briefing/")}

    # Then
    for key, body in proxied.items():
        assert f"proxy_pass {RAG_UPSTREAM};" in body, key
        for directive in REQUIRED_DIRECTIVES:
            assert directive in body, (key, directive)
        assert "try_files" not in body, key


def test_proxy_pass_keeps_request_path_unchanged() -> None:
    # Given
    conf = NGINX_CONF.read_text(encoding="utf-8")

    # When
    upstreams = re.findall(r"proxy_pass\s+([^;]+);", conf)

    # Then: no URI part after host:port, so nginx forwards the original path verbatim
    assert upstreams == [RAG_UPSTREAM, RAG_UPSTREAM]


def test_spa_fallback_and_health_stay_local() -> None:
    # Given
    blocks = locations(NGINX_CONF.read_text(encoding="utf-8"))

    # When
    root_block = blocks.get("/", "")

    # Then
    assert "try_files $uri $uri/ /index.html;" in root_block
    assert "proxy_pass" not in root_block
    assert not any("health" in key for key in blocks)


def test_web_service_starts_after_rag_api() -> None:
    # Given
    compose = COMPOSE.read_text(encoding="utf-8")

    # When
    web = re.search(r"^  web:\n((?:    .*\n|\n)+?)(?=^  \S)", compose, re.MULTILINE)

    # Then: nginx resolves rag-api at startup, so the service must exist first
    assert web is not None
    assert re.search(r"depends_on:\n\s+- rag-api", web.group(1))
