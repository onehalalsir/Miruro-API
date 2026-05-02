import base64, json, gzip, httpx, os
from urllib.parse import urljoin
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Miruro API", version="2.1")

# --- CORS ---
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
    "Origin": "https://www.miruro.online"
}

ANILIST_URL = "https://graphql.anilist.co"
MIRURO_PIPE_URL = "https://www.miruro.online/api/secure/pipe"

# ─── BASIC ─────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home():
    return "<h1>Miruro API Running ✅</h1>"

# ─── UTIL ──────────────────────────────────

def _encode(payload):
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

def _decode(s):
    s += "=" * (4 - len(s) % 4)
    return json.loads(gzip.decompress(base64.urlsafe_b64decode(s)).decode())

async def _pipe(payload):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{MIRURO_PIPE_URL}?e={_encode(payload)}",
            headers=HEADERS
        )
    if r.status_code != 200:
        raise HTTPException(500, "Pipe failed")
    return _decode(r.text.strip())

# ─── EPISODES ──────────────────────────────

@app.get("/episodes/{anilist_id}")
async def episodes(anilist_id: int):
    data = await _pipe({
        "path": "episodes",
        "method": "GET",
        "query": {"anilistId": anilist_id},
        "body": None,
        "version": "0.1.0",
    })

    # inject watch slugs
    for prov, pdata in data.get("providers", {}).items():
        for cat, eps in pdata.get("episodes", {}).items():
            for ep in eps:
                if "id" in ep and "number" in ep:
                    prefix = ep["id"].split(":")[0]
                    ep["id"] = f"watch/{prov}/{anilist_id}/{cat}/{prefix}-{ep['number']}"

    return data

# ─── SOURCES ───────────────────────────────

@app.get("/watch/{provider}/{anilist_id}/{category}/{slug}")
async def watch(provider: str, anilist_id: int, category: str, slug: str):

    data = await episodes(anilist_id)
    eps = data["providers"][provider]["episodes"][category]

    target = None
    for ep in eps:
        prefix = ep["id"].split("/")[-1].split("-")[0]
        if f"{prefix}-{ep['number']}" == slug:
            target = ep["id"]
            break

    if not target:
        raise HTTPException(404, "Episode not found")

    return await sources(target, provider, anilist_id, category)


@app.get("/sources")
async def sources(
    episodeId: str,
    provider: str,
    anilistId: int,
    category: str = "sub"
):
    enc = base64.urlsafe_b64encode(episodeId.encode()).decode().rstrip("=")

    return await _pipe({
        "path": "sources",
        "method": "GET",
        "query": {
            "episodeId": enc,
            "provider": provider,
            "category": category,
            "anilistId": anilistId,
        },
        "body": None,
        "version": "0.1.0",
    })

# ─── 🔥 HLS PROXY (FIXES YOUR ISSUE) ───────

@app.get("/hls-proxy")
async def hls_proxy(url: str):
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(url, headers=HEADERS)

    content_type = r.headers.get("content-type", "")

    # rewrite playlist
    if ".m3u8" in url or "mpegurl" in content_type:
        base = url.rsplit("/", 1)[0] + "/"
        new_lines = []

        for line in r.text.splitlines():
            if line and not line.startswith("#"):
                full = urljoin(base, line)
                line = f"/hls-proxy?url={full}"
            new_lines.append(line)

        return Response(
            "\n".join(new_lines),
            media_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*"}
        )

    return Response(
        r.content,
        media_type=content_type or "application/octet-stream",
        headers={"Access-Control-Allow-Origin": "*"}
    )
