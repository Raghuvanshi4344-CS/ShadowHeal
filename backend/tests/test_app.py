"""Application foundation tests for ShadowHeal FastAPI backend (SH-R04)."""

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from backend.app.config import Settings
from backend.app.main import app

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    """Create FastAPI TestClient fixture."""
    with TestClient(app) as test_client:
        yield test_client


def test_app_initialization():
    """Verify application metadata and routing table."""
    assert app.title == "ShadowHeal API"
    assert app.version == "0.1.0"
    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"

    # Verify /health route is registered
    routes = [route.path for route in app.routes]
    assert "/health" in routes
    assert "/docs" in routes
    assert "/openapi.json" in routes


def test_health_check_endpoint(client: TestClient):
    """Verify GET /health returns HTTP 200 and exact expected payload."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "shadowheal-api",
    }


def test_swagger_documentation_accessible(client: TestClient):
    """Verify Swagger UI is accessible at /docs."""
    response = client.get("/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text.lower() or "html" in response.headers.get("content-type", "").lower()


def test_openapi_specification_accessible(client: TestClient):
    """Verify OpenAPI JSON specification is valid and contains correct metadata."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    data = response.json()
    assert data["info"]["title"] == "ShadowHeal API"
    assert data["info"]["version"] == "0.1.0"
    assert "/health" in data["paths"]


def test_settings_load_and_validation():
    """Verify Settings load correctly and validate constraints."""
    # Standard settings load
    settings = Settings(
        REDIS_HOST="127.0.0.1",
        REDIS_PORT=6379,
        REDIS_QUEUE_NAME="shadowheal:events:raw",
    )
    assert settings.REDIS_HOST == "127.0.0.1"
    assert settings.REDIS_PORT == 6379
    assert settings.REDIS_QUEUE_NAME == "shadowheal:events:raw"

    # Unrelated env vars are ignored
    settings_with_extra = Settings(
        POSTGRES_USER="db_user",
        POSTGRES_PASSWORD="db_password",
    )
    assert settings_with_extra.REDIS_PORT == 6379

    # Invalid port (< 1 or > 65535)
    with pytest.raises(ValidationError):
        Settings(REDIS_PORT=0)
    with pytest.raises(ValidationError):
        Settings(REDIS_PORT=70000)

    # Empty queue name
    with pytest.raises(ValidationError):
        Settings(REDIS_QUEUE_NAME="")
