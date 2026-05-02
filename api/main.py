import base64
import json
import gzip
import httpx
from urllib.parse import urljoin
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Miruro API", version="2.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.miruro.online/",
    "Origin": "https://www.miruro.online",
}

ANILIST_URL = "https://graphql.anilist.co"
MIRURO_PIPE_URL = "https://www.miruro.online/api/secure/pipe"


@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <h1>Miruro API Running ✅</h1>
    <p>Use /episodes/{anilist_id}, /watch/{provider}/{anilist_id}/{category}/{slug}, or /hls-proxy?url=...</p>
    """


def _encode_pipe_request(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode().rstrip("=")


def _decode_pipe_response(encoded_str: str) -> dict:
    try:
        encoded_str += "=" * (4 - len(encoded_str) % 4)
        compressed = base64.urlsafe_b64decode(encoded_str)
        return json.loads(gzip.decompress(compressed).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decode pipe response")


async def _pipe(payload: dict):
    encoded = _encode_pipe_request(payload)

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        res = await client.get(
            f"{MIRURO_PIPE_URL}?e={encoded}",
            headers=HEADERS,
        )

    if res.status_code != 200:
        raise HTTPException(
            status_code=res.status_code,
            detail=f"Pipe request failed: {res.text[:300]}",
        )

    return _decode_pipe_response(res.text.strip())


def _translate_id(encoded_id: str) -> str:
    try:
        decoded = base64.urlsafe_b64decode(
            encoded_id + "=" * (4 - len(encoded_id) % 4)
        ).decode()

        if ":" in decoded:
            return decoded

        return encoded_id
    except Exception:
        return encoded_id


def _deep_translate(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "id" and isinstance(value, str):
                obj[key] = _translate_id(value)
            elif isinstance(value, (dict, list)):
                _deep_translate(value)

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _deep_translate(item)


def _inject_source_slugs(data: dict, anilist_id: int):
    providers = data.get("providers", {})

    for provider_name, provider_data in providers.items():
        if not isinstance(provider_data, dict):
            continue

        episodes = provider_data.get("episodes", {})

        if isinstance(episodes, list):
            provider_data["episodes"] = {"sub": episodes}
            episodes = provider_data["episodes"]

        if not isinstance(episodes, dict):
            continue

        for category, ep_list in episodes.items():
            if not isinstance(ep_list, list):
                continue

            for ep in ep_list:
                if not isinstance(ep, dict):
                    continue

                if "id" not in ep or "number" not in ep:
                    continue

                raw_id = ep["id"]
                ep["rawId"] = raw_id

                prefix = raw_id.split(":")[0] if ":" in raw_id else raw_id
                ep["id"] = f"watch/{provider_name}/{anilist_id}/{category}/{prefix}-{ep['number']}"

    return data


async def _fetch_raw_episodes(anilist_id: int) -> dict:
    data = await _pipe({
        "path": "episodes",
        "method": "GET",
        "query": {"anilistId": anilist_id},
        "body": None,
        "version": "0.1.0",
    })

    _deep_translate(data)
    return data


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
    encoded_episode_id = base64.urlsafe_b64encode(
        episodeId.encode()
    ).decode().rstrip("=")

    return await _pipe({
        "path": "sources",
        "method": "GET",
        "query": {
            "episodeId": encoded_episode_id,
            "provider": provider,
            "category": category,
            "anilistId": anilistId,
        },
        "body": None,
        "version": "0.1.0",
    })


@app.get("/watch/{provider}/{anilist_id}/{category}/{slug}")
async def get_watch_sources(
    provider: str,
    anilist_id: int,
    category: str,
    slug: str,
):
    data = await get_episodes(anilist_id)

    provider_data = data.get("providers", {}).get(provider)
    if not provider_data:
        raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

    episodes = provider_data.get("episodes", {}).get(category, [])

    target_id = None

    for ep in episodes:
        raw_id = ep.get("rawId")
        number = ep.get("number")

        if raw_id is None or number is None:
            continue

        prefix = raw_id.split(":")[0] if ":" in raw_id else raw_id
        generated_slug = f"{prefix}-{number}"

        if generated_slug == slug:
            target_id = raw_id
            break

    if not target_id:
        raise HTTPException(
            status_code=404,
            detail=f"Episode slug not found: {slug}",
        )

    return await get_sources(
        episodeId=target_id,
        provider=provider,
        anilistId=anilist_id,
        category=category,
    )


@app.get("/hls-proxy")
async def hls_proxy(url: str):
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        res = await client.get(url, headers=HEADERS)

    if res.status_code >= 400:
        raise HTTPException(
            status_code=res.status_code,
            detail=f"HLS fetch failed: {res.text[:200]}",
        )

    content_type = res.headers.get("content-type", "")

    if ".m3u8" in url or "mpegurl" in content_type.lower():
        base = url.rsplit("/", 1)[0] + "/"
        rewritten_lines = []

        for line in res.text.splitlines():
            clean = line.strip()

            if not clean:
                rewritten_lines.append(line)
                continue

            if clean.startswith("#"):
                if clean.startswith("#EXT-X-KEY") and 'URI="' in clean:
                    before, rest = clean.split('URI="', 1)
                    key_url, after = rest.split('"', 1)
                    full_key_url = urljoin(base, key_url)
                    clean = f'{before}URI="/hls-proxy?url={full_key_url}"{after}'

                rewritten_lines.append(clean)
                continue

            full_url = urljoin(base, clean)
            rewritten_lines.append(f"/hls-proxy?url={full_url}")

        return Response(
            "\n".join(rewritten_lines),
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )

    return Response(
        res.content,
        media_type=content_type or "application/octet-stream",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
        },
    )
