import asyncio
import os
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from urllib.parse import urlparse
import yt_dlp

router = APIRouter()

COOKIE_FILE = '/root/yt-cookies.txt'
COOKIE_MAX_AGE_DAYS = 21

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

def get_cookie_status() -> dict:
    if not os.path.exists(COOKIE_FILE):
        return {'exists': False, 'age_days': None, 'needs_refresh': True}
    age_days = (time.time() - os.path.getmtime(COOKIE_FILE)) / 86400
    return {
        'exists': True,
        'age_days': round(age_days, 1),
        'needs_refresh': age_days > COOKIE_MAX_AGE_DAYS,
    }

def _resolve_sync(url: str) -> dict:
    opts = {
        'format': 'best[vcodec^=avc1]/18/best',
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 15,
        'noplaylist': True,
        'js_runtimes': {'node': {'path': '/usr/bin/node'}},
        'extractor_args': {
            'youtubepot-bgutilhttp': {
                'base_url': 'http://127.0.0.1:4416',
            }
        },
    }

    cookie_status = get_cookie_status()
    if cookie_status['exists']:
        opts['cookiefile'] = COOKIE_FILE

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if 'entries' in info:
            info = info['entries'][0]

        requested = info.get('requested_formats')
        if requested and len(requested) == 2:
            video_fmt = next((f for f in requested if f.get('vcodec') != 'none'), None)
            audio_fmt = next((f for f in requested if f.get('vcodec') == 'none'), None)
            resolved = video_fmt['url'] if video_fmt else info['url']
            audio_url = audio_fmt['url'] if audio_fmt else None
        else:
            resolved = info['url']
            audio_url = None

        return {
            'resolved_url': resolved,
            'audio_url': audio_url,
            'title': info.get('title') or '',
            'duration': info.get('duration') or 0,
            'is_live': bool(info.get('is_live', False)),
            'thumbnail': info.get('thumbnail') or '',
            'quality': info.get('height') or 0,
            'is_dash': False,
        }

@router.get("/health")
def stream_health():
    return {"ok": True}

@router.get("/cookie-status")
def cookie_status():
    return get_cookie_status()

@router.post("/resolve")
async def resolve(req: ResolveRequest):
    url = req.url.strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL is empty")

    if is_blocked(url):
        raise HTTPException(status_code=403, detail="SITE_BLOCKED")

    if not needs_resolution(url):
        return {
            'resolved_url': url,
            'audio_url': None,
            'title': '',
            'duration': 0,
            'is_live': False,
            'thumbnail': '',
            'quality': 0,
            'is_dash': False,
        }

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _resolve_sync, url),
            timeout=30.0
        )
        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="TIMEOUT")
    except Exception as e:
        err = str(e)
        if 'Sign in to confirm' in err or 'bot' in err.lower():
            raise HTTPException(status_code=403, detail="YOUTUBE_BOT_DETECTED")
        if 'Private video' in err:
            raise HTTPException(status_code=403, detail="VIDEO_PRIVATE")
        if 'unavailable' in err.lower():
            raise HTTPException(status_code=404, detail="VIDEO_UNAVAILABLE")
        raise HTTPException(status_code=422, detail=err)