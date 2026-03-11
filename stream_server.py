import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from urllib.parse import urlparse
import yt_dlp

router = APIRouter()

BLOCKED_DOMAINS = [
    'pornhub.com', 'xvideos.com', 'xhamster.com', 'onlyfans.com',
    'redtube.com', 'xnxx.com', 'spankbang.com', 'youporn.com',
]

NEEDS_RESOLVER = [
    'youtube.com', 'youtu.be', 'twitch.tv', 'vimeo.com',
    'dailymotion.com', 'twitter.com', 'x.com', 'tiktok.com',
    'bilibili.com', 'nicovideo.jp', 'reddit.com',
]

class ResolveRequest(BaseModel):
    url: str

def get_domain(url: str) -> str:
    return urlparse(url).netloc.lower().replace('www.', '')

def is_blocked(url: str) -> bool:
    domain = get_domain(url)
    return any(b in domain for b in BLOCKED_DOMAINS)

def needs_resolution(url: str) -> bool:
    domain = get_domain(url)
    return any(n in domain for n in NEEDS_RESOLVER)

def _resolve_sync(url: str) -> dict:
    opts = {
        'format': 'best[ext=mp4][protocol=https]/best[protocol=m3u8]/best',
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 15,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if 'entries' in info:
            info = info['entries'][0]
        return {
            'resolved_url': info['url'],
            'title': info.get('title') or '',
            'duration': info.get('duration') or 0,
            'is_live': bool(info.get('is_live', False)),
            'thumbnail': info.get('thumbnail') or '',
        }

@router.get("/health")
def stream_health():
    return {"ok": True}

@router.post("/resolve")
async def resolve(req: ResolveRequest):
    url = req.url.strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL is empty")

    if is_blocked(url):
        raise HTTPException(status_code=403, detail="This site is not allowed")

    if not needs_resolution(url):
        return {
            'resolved_url': url,
            'title': '',
            'duration': 0,
            'is_live': False,
            'thumbnail': '',
        }

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _resolve_sync, url),
            timeout=30.0
        )
        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Resolution timed out")
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))