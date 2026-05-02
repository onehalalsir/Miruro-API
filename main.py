import base64, json, gzip, httpx, os
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Miruro API", version="2.0")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Security Configuration ---
API_KEY_NAME = "x-api-key"
VALID_API_KEY = os.getenv("API_KEY")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://www.miruro.online/"}
ANILIST_URL = "https://graphql.anilist.co"
MIRURO_PIPE_URL = "https://www.miruro.online/api/secure/pipe"

def _proxy_img(url: str) -> str:
    return url


def _proxy_deep_images(obj):
    return obj

def _inject_source_slugs(data: dict, anilist_id: int):
    """Transform episode IDs into simplified path-based slugs: watch/PROV/ALID/CAT/PREFIX-NUMBER"""
    providers = data.get("providers", {})
    for provider_name, provider_data in providers.items():
        if not isinstance(provider_data, dict):
            continue
        episodes = provider_data.get("episodes", {})
        if not isinstance(episodes, dict):
            if isinstance(episodes, list):
                provider_data["episodes"] = {"sub": episodes}
                episodes = provider_data["episodes"]
            else:
                continue
        for category, ep_list in episodes.items():
            if not isinstance(ep_list, list):
                continue
            for ep in ep_list:
                if not isinstance(ep, dict):
                    continue
                if "id" in ep and "number" in ep:
                    orig_id = ep["id"]
                    prefix = orig_id.split(":")[0] if ":" in orig_id else orig_id
                    ep["id"] = f"watch/{provider_name}/{anilist_id}/{category}/{prefix}-{ep['number']}"
    return data

async def _fetch_raw_episodes(anilist_id: int) -> dict:
    payload = {
        "path": "episodes",
        "method": "GET",
        "query": {"anilistId": anilist_id},
        "body": None,
        "version": "0.1.0",
    }
    encoded_req = _encode_pipe_request(payload)
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{MIRURO_PIPE_URL}?e={encoded_req}", headers=HEADERS)
        if res.status_code != 200:
            raise HTTPException(status_code=res.status_code, detail="Pipe request failed")
        data = _decode_pipe_response(res.text.strip())
        _deep_translate(data)
        return data

# ─── Shared GraphQL Fragments ────────────────────────────────────────────────

MEDIA_LIST_FIELDS = """
    id
    title { romaji english native }
    coverImage { large extraLarge }
    bannerImage
    format
    season
    seasonYear
    episodes
    duration
    status
    averageScore
    meanScore
    popularity
    favourites
    genres
    source
    countryOfOrigin
    isAdult
    studios(isMain: true) { nodes { name isAnimationStudio } }
    nextAiringEpisode { episode airingAt timeUntilAiring }
    startDate { year month day }
    endDate { year month day }
"""

MEDIA_FULL_FIELDS = """
    id
    idMal
    title { romaji english native }
    description(asHtml: false)
    coverImage { large extraLarge color }
    bannerImage
    format
    season
    seasonYear
    episodes
    duration
    status
    averageScore
    meanScore
    popularity
    favourites
    trending
    genres
    tags { name rank isMediaSpoiler }
    source
    countryOfOrigin
    isAdult
    hashtag
    synonyms
    siteUrl
    trailer { id site thumbnail }
    studios { nodes { id name isAnimationStudio siteUrl } }
    nextAiringEpisode { episode airingAt timeUntilAiring }
    startDate { year month day }
    endDate { year month day }
    characters(sort: [ROLE, RELEVANCE], perPage: 25) {
        edges {
            role
            node { id name { full native } image { large } }
            voiceActors(language: JAPANESE) { id name { full native } image { large } languageV2 }
        }
    }
    staff(sort: RELEVANCE, perPage: 25) {
        edges {
            role
            node { id name { full native } image { large } }
        }
    }
    relations {
        edges {
            relationType(version: 2)
            node {
                id
                title { romaji english native }
                coverImage { large }
                format
                type
                status
                episodes
                meanScore
            }
        }
    }
    recommendations(sort: RATING_DESC, perPage: 10) {
        nodes {
            rating
            mediaRecommendation {
                id
                title { romaji english native }
                coverImage { large }
                format
                episodes
                status
                meanScore
                averageScore
            }
        }
    }
    externalLinks { url site type }
    streamingEpisodes { title thumbnail url site }
    stats {
        scoreDistribution { score amount }
        statusDistribution { status amount }
    }
"""

# ─── Utility Functions ───────────────────────────────────────────────────────

def _translate_id(encoded_id: str) -> str:
    try:
        decoded = base64.urlsafe_b64decode(encoded_id + '=' * (4 - len(encoded_id) % 4)).decode()
        if ':' in decoded:
            return decoded
        return encoded_id
    except Exception:
        return encoded_id


def _deep_translate(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == 'id' and isinstance(value, str):
                obj[key] = _translate_id(value)
            elif isinstance(value, (dict, list)):
                _deep_translate(value)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _deep_translate(item)


def _decode_pipe_response(encoded_str: str) -> dict:
    try:
        encoded_str += '=' * (4 - len(encoded_str) % 4)
        compressed = base64.urlsafe_b64decode(encoded_str)
        return json.loads(gzip.decompress(compressed).decode('utf-8'))
    except Exception:
        raise ValueError("Failed to decode pipe response")


def _encode_pipe_request(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')


async def _anilist_query(query: str, variables: dict = None):
    body = {"query": query}
    if variables:
        body["variables"] = variables
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(ANILIST_URL, json=body)
        if res.status_code != 200:
            raise HTTPException(status_code=500, detail="AniList query failed")
        return res.json().get("data", {})


# ─── Homepage ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Miruro API v2.0</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Outfit', sans-serif; transition: all 0.3s ease; }
        body { background: radial-gradient(circle at top, #0f172a, #020617); color: #e2e8f0; min-height: 100vh; padding: 50px 20px; }
        .container { max-width: 960px; margin: 0 auto; background: rgba(30, 41, 59, 0.5); backdrop-filter: blur(10px); padding: 40px; border-radius: 24px; border: 1px solid rgba(255, 255, 255, 0.05); box-shadow: 0 20px 40px rgba(0,0,0,0.5); }
        .header { text-align: center; margin-bottom: 50px; }
        .logo { width: 120px; border-radius: 20px; box-shadow: 0 0 30px rgba(56, 189, 248, 0.3); border: 1px solid rgba(255,255,255,0.1); margin-bottom: 25px; object-fit: cover; }
        h1 { font-size: 3em; font-weight: 700; background: linear-gradient(to right, #38bdf8, #818cf8); -webkit-background-clip: text; color: transparent; margin-bottom: 10px; }
        .subtitle { color: #94a3b8; font-size: 1.1em; font-weight: 300; }
        .version { display: inline-block; background: rgba(56, 189, 248, 0.15); color: #38bdf8; padding: 4px 14px; border-radius: 20px; font-size: 0.85em; margin-top: 10px; border: 1px solid rgba(56, 189, 248, 0.2); }
        .section-title { font-size: 1.3em; font-weight: 700; color: #818cf8; margin: 35px 0 15px; border-left: 3px solid #818cf8; padding-left: 12px; }
        .endpoint { background: rgba(15, 23, 42, 0.8); border-left: 4px solid #38bdf8; padding: 25px; margin: 15px 0; border-radius: 0 16px 16px 0; border: 1px solid rgba(255,255,255,0.02); }
        .endpoint:hover { transform: translateX(5px); box-shadow: 0 10px 20px rgba(0,0,0,0.2); border-left-color: #818cf8; background: rgba(30, 41, 59, 0.9); }
        .method { color: #10b981; font-weight: 700; background: rgba(16, 185, 129, 0.1); padding: 4px 10px; border-radius: 6px; font-size: 0.9em; margin-right: 10px; }
        .url { font-family: monospace; color: #cbd5e1; font-size: 1.1em; }
        .params { margin-top: 10px; font-size: 0.85em; color: #64748b; font-family: monospace; line-height: 1.8; }
        .params span { color: #a5b4fc; }
        .example { margin-top: 15px; font-size: 0.95em; color: #64748b; }
        a { color: #38bdf8; text-decoration: none; word-break: break-all; font-weight: 500; }
        a:hover { color: #818cf8; text-shadow: 0 0 10px rgba(129, 140, 248, 0.5); }
        .desc { color: #cbd5e1; font-size: 1em; margin-top: 10px; font-weight: 300; line-height: 1.6; }
        .footer { text-align: center; margin-top: 50px; color: #475569; font-size: 0.9em; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <img src="https://www.miruro.to/icon-512x512.png" alt="Logo" class="logo">
            <h1>Miruro Native API</h1>
            <div class="subtitle">Decrypted, bypassed, and reverse-engineered anime streaming API</div>
            <div class="version">v2.0 — Full Data &amp; Pagination</div>
        </div>
        <div class="footer">
            Developed by Walter | <a href="https://github.com/walterwhite-69" target="_blank">github.com/walterwhite-69</a>
        </div>
    </div>
</body>
</html>"""


# ─── Search & Suggestions ───────────────────────────────────────────────────

@app.get("/search")
async def search_anime(
    query: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    gql = f"""
    query ($search: String, $page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, {"search": query, "page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    return {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }


@app.get("/suggestions")
async def search_suggestions(query: str = Query(..., min_length=1)):
    gql = """
    query ($search: String) {
        Page(page: 1, perPage: 8) {
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
                id
                title { romaji english }
                coverImage { large }
                format
                status
                startDate { year }
                episodes
            }
        }
    }
    """
    data = await _anilist_query(gql, {"search": query})
    results = []
    for item in data.get("Page", {}).get("media", []):
        results.append({
            "id": item["id"],
            "title": item["title"].get("english") or item["title"].get("romaji"),
            "title_romaji": item["title"].get("romaji"),
            "poster": item["coverImage"]["large"],
            "format": item.get("format"),
            "status": item.get("status"),
            "year": (item.get("startDate") or {}).get("year"),
            "episodes": item.get("episodes"),
        })
    return {"suggestions": results}


# ─── Advanced Filter ─────────────────────────────────────────────────────────

SORT_MAP = {
    "SCORE_DESC": "SCORE_DESC",
    "POPULARITY_DESC": "POPULARITY_DESC",
    "TRENDING_DESC": "TRENDING_DESC",
    "START_DATE_DESC": "START_DATE_DESC",
    "FAVOURITES_DESC": "FAVOURITES_DESC",
    "UPDATED_AT_DESC": "UPDATED_AT_DESC",
}

@app.get("/filter")
async def filter_anime(
    genre: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    season: Optional[str] = Query(None),
    format: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    sort: str = Query("POPULARITY_DESC"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    args = ["type: ANIME", f"sort: [{SORT_MAP.get(sort, 'POPULARITY_DESC')}]"]
    variables = {"page": page, "perPage": per_page}
    var_types = ["$page: Int", "$perPage: Int"]

    if genre:   args.append("genre: $genre");       variables["genre"] = genre;             var_types.append("$genre: String")
    if tag:     args.append("tag: $tag");            variables["tag"] = tag;                 var_types.append("$tag: String")
    if year:    args.append("seasonYear: $seasonYear"); variables["seasonYear"] = year;      var_types.append("$seasonYear: Int")
    if season:  args.append("season: $season");      variables["season"] = season.upper();   var_types.append("$season: MediaSeason")
    if format:  args.append("format: $format");      variables["format"] = format.upper();   var_types.append("$format: MediaFormat")
    if status:  args.append("status: $status");      variables["status"] = status.upper();   var_types.append("$status: MediaStatus")

    gql = f"""
    query ({', '.join(var_types)}) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media({', '.join(args)}) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, variables)
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    return {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }


# ─── Collections ─────────────────────────────────────────────────────────────

async def _fetch_collection(sort_type: str, status: str = None, page: int = 1, per_page: int = 20):
    status_filter = f", status: {status}" if status else ""
    gql = f"""
    query ($page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media(type: ANIME, sort: [{sort_type}]{status_filter}) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, {"page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    return {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }


@app.get("/spotlight")
async def get_spotlight():
    gql = f"""
    query {{
        Page(page: 1, perPage: 10) {{
            media(sort: [TRENDING_DESC, POPULARITY_DESC], type: ANIME) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql)
    return {"results": data.get("Page", {}).get("media", [])}


@app.get("/trending")
async def get_trending(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):
    return await _fetch_collection("TRENDING_DESC", page=page, per_page=per_page)


@app.get("/popular")
async def get_popular(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):
    return await _fetch_collection("POPULARITY_DESC", page=page, per_page=per_page)


@app.get("/upcoming")
async def get_upcoming(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):
    return await _fetch_collection("POPULARITY_DESC", "NOT_YET_RELEASED", page=page, per_page=per_page)


@app.get("/recent")
async def get_recent(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):
    return await _fetch_collection("START_DATE_DESC", "RELEASING", page=page, per_page=per_page)


@app.get("/schedule")
async def get_schedule(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):
    gql = f"""
    query ($page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            airingSchedules(notYetAired: true, sort: TIME) {{
                episode
                airingAt
                timeUntilAiring
                media {{
                    {MEDIA_LIST_FIELDS}
                }}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, {"page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    results = []
    for item in page_data.get("airingSchedules", []):
        entry = item.get("media", {})
        entry["next_episode"] = item.get("episode")
        entry["airingAt"] = item.get("airingAt")
        entry["timeUntilAiring"] = item.get("timeUntilAiring")
        results.append(entry)
    return {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": results,
    }


# ─── Anime Details ───────────────────────────────────────────────────────────

@app.get("/info/{anilist_id}")
async def get_anime_info(anilist_id: int):
    gql = f"""
    query ($id: Int) {{
        Media(id: $id, type: ANIME) {{
            {MEDIA_FULL_FIELDS}
        }}
    }}
    """
    data = await _anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    return media


@app.get("/anime/{anilist_id}/characters")
async def get_anime_characters(anilist_id: int, page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=50)):
    gql = """
    query ($id: Int, $page: Int, $perPage: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            characters(sort: [ROLE, RELEVANCE], page: $page, perPage: $perPage) {
                pageInfo { total currentPage lastPage hasNextPage perPage }
                edges {
                    role
                    node { id name { full native } image { large } }
                    voiceActors { id name { full native } image { large } languageV2 }
                }
            }
        }
    }
    """
    data = await _anilist_query(gql, {"id": anilist_id, "page": page, "perPage": per_page})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    chars = media.get("characters", {})
    page_info = chars.get("pageInfo", {})
    return {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "characters": chars.get("edges", []),
    }


@app.get("/anime/{anilist_id}/relations")
async def get_anime_relations(anilist_id: int):
    gql = """
    query ($id: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            relations {
                edges {
                    relationType(version: 2)
                    node {
                        id title { romaji english native }
                        coverImage { large } bannerImage
                        format type status episodes chapters
                        meanScore averageScore popularity
                        startDate { year month day }
                    }
                }
            }
        }
    }
    """
    data = await _anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    return {"id": media["id"], "title": media["title"], "relations": media.get("relations", {}).get("edges", [])}


@app.get("/anime/{anilist_id}/recommendations")
async def get_anime_recommendations(anilist_id: int, page: int = Query(1, ge=1), per_page: int = Query(10, ge=1, le=25)):
    gql = """
    query ($id: Int, $page: Int, $perPage: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            recommendations(sort: RATING_DESC, page: $page, perPage: $perPage) {
                pageInfo { total currentPage lastPage hasNextPage perPage }
                nodes {
                    rating
                    mediaRecommendation {
                        id title { romaji english native }
                        coverImage { large extraLarge } bannerImage
                        format episodes status meanScore averageScore popularity genres
                        startDate { year }
                    }
                }
            }
        }
    }
    """
    data = await _anilist_query(gql, {"id": anilist_id, "page": page, "perPage": per_page})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    recs = media.get("recommendations", {})
    page_info = recs.get("pageInfo", {})
    return {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "recommendations": recs.get("nodes", []),
    }


# ─── Streaming ───────────────────────────────────────────────────────────────

@app.get("/episodes/{anilist_id}")
async def get_episodes(anilist_id: int):
    data = await _fetch_raw_episodes(anilist_id)
    return _inject_source_slugs(data, anilist_id)


@app.get("/sources")
async def get_sources(
    episodeId: str = Query(...),
    provider: str = Query(...),
    anilistId: int = Query(...),
    category: str = Query("sub"),
):
    enc_id = base64.urlsafe_b64encode(episodeId.encode()).decode().rstrip('=')
    payload = {
        "path": "sources",
        "method": "GET",
        "query": {
            "episodeId": enc_id,
            "provider": provider,
            "category": category,
            "anilistId": anilistId,
        },
        "body": None,
        "version": "0.1.0",
    }
    encoded_req = _encode_pipe_request(payload)
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(f"{MIRURO_PIPE_URL}?e={encoded_req}", headers=HEADERS)
        if res.status_code != 200:
            raise HTTPException(status_code=res.status_code, detail="Pipe request failed")
        return _decode_pipe_response(res.text.strip())


@app.get("/watch/{provider}/{anilist_id}/{category}/{slug}")
async def get_watch_sources(provider: str, anilist_id: int, category: str, slug: str):
    data = await _fetch_raw_episodes(anilist_id)
    prov_data = data.get("providers", {}).get(provider, {})
    ep_list = prov_data.get("episodes", {}).get(category, [])

    target_id = None
    for ep in ep_list:
        orig_id = ep.get("id", "")
        prefix = orig_id.split(":")[0] if ":" in orig_id else orig_id
        generated = f"{prefix}-{ep.get('number')}"
        if generated == slug:
            target_id = orig_id
            break

    if not target_id:
        raise HTTPException(status_code=404, detail=f"Episode slug '{slug}' not found for provider {provider}")

    return await get_sources(episodeId=target_id, provider=provider, anilistId=anilist_id, category=category)
