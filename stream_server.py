import asyncio
import urllib.request
import json as jsonlib
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from urllib.parse import urlparse, parse_qs
import yt_dlp
from typing import Optional

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
        'format': 'bestvideo[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]/18/best',
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 15,
        'noplaylist': True,
        'js_runtimes': 'node:/usr/bin/node',
        'extractor_args': {
            'youtubepot-bgutilhttp': {
                'base_url': 'http://127.0.0.1:4416',
            }
        },
    }
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