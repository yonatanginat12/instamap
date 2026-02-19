# Discover

Explore Instagram posts and places (restaurants & things to do) for any location.
Pulls Instagram content via **Instaloader**, enriches with **Yelp** and **Foursquare**.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
cp .env.example .env   # then fill in your API keys
```

## API Keys (free)

| Service | Limit | Link |
|---------|-------|------|
| Yelp Fusion | 500 req/day | https://www.yelp.com/developers/v3/manage_app |
| Foursquare Places | 1 000 req/day | https://developer.foursquare.com/ |

Instagram is accessed via Instaloader (no key required). For more results, add
your Instagram credentials to `.env` (optional).

## Run

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000 in your browser.

## Commands

| Task | Command |
|------|---------|
| Start server | `uvicorn app.main:app --reload` |
| Run tests | `pytest` |
| Lint | `ruff check src tests` |
| Format | `ruff format src tests` |

## Project layout

```
src/app/
  main.py          FastAPI app + routes
  instagram.py     Instaloader hashtag search
  yelp.py          Yelp Fusion API client
  foursquare.py    Foursquare Places API client
  models.py        Pydantic response models
templates/
  index.html       Single-page web UI
tests/
  test_health.py
  test_search.py
.env.example       API key template
```
