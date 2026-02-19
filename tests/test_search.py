from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _empty_ig():
    return ([], [])


def _empty_yelp():
    return ([], ["Yelp: set YELP_API_KEY in .env to enable restaurant results"])


def _empty_osm():
    return ([], [], [])


def test_search_returns_structure():
    with (
        patch("app.main.search_instagram", new=AsyncMock(return_value=_empty_ig())),
        patch("app.main.search_yelp", new=AsyncMock(return_value=_empty_yelp())),
        patch("app.main.search_osm", new=AsyncMock(return_value=_empty_osm())),
    ):
        resp = client.get(
            "/api/search", params={"location": "Tel Aviv", "category": "all"}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["location"] == "Tel Aviv"
    assert body["category"] == "all"
    assert "instagram_posts" in body
    assert "yelp_businesses" in body
    assert "osm_restaurants" in body
    assert "osm_activities" in body
    assert isinstance(body["warnings"], list)


def test_search_invalid_category():
    resp = client.get(
        "/api/search", params={"location": "Paris", "category": "invalid"}
    )
    assert resp.status_code == 422


def test_search_missing_location():
    resp = client.get("/api/search")
    assert resp.status_code == 422
