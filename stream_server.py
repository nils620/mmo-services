import asyncio
import urllib.request
import json as jsonlib
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from urllib.parse import urlparse, parse_qs
import yt_dlp
from typing import Optional

router = APIRouter()

PIPED_API = "https://pipedapi.syncpundit.io/"

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

def _extract_youtube_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    # youtube.com/watch?v=xxx
    qs = parse_qs(parsed.query)
    if 'v' in qs:
        return qs['v'][0]
    # youtu.be/xxx
    if 'youtu.be' in parsed.netloc:
        return parsed.path.lstrip('/')
    return None

def _resolve_youtube_piped(video_id: str) -> dict:
    req = urllib.request.Request(
        f"{PIPED_API}/streams/{video_id}",
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = jsonlib.loads(resp.read())

    # Pick best mp4 video stream with H.264
    video_streams = [
        s for s in data.get('videoStreams', [])
        if 'avc1' in s.get('codec', '') and s.get('mimeType') == 'video/mp4'
    ]
    # Pick best m4a audio stream
    audio_streams = [
        s for s in data.get('audioStreams', [])
        if s.get('mimeType') == 'audio/mp4'
    ]

    # Sort by quality descending, cap at 1080p
    video_streams.sort(key=lambda s: s.get('quality', 0), reverse=True)
    audio_streams.sort(key=lambda s: s.get('bitrate', 0), reverse=True)

    # Filter to max 1080p
    video_streams = [s for s in video_streams if s.get('height', 0) <= 1080]

    if not video_streams:
        raise Exception("No compatible video stream found via Piped")

    video_url = video_streams[0]['url']
    audio_url = audio_streams[0]['url'] if audio_streams else None

    return {
        'resolved_url': video_url,
        'audio_url': audio_url,
        'title': data.get('title') or '',
        'duration': data.get('duration') or 0,
        'is_live': bool(data.get('livestream', False)),
        'thumbnail': data.get('thumbnailUrl') or '',
        'quality': video_streams[0].get('height') or 0,
        'is_dash': False,
    }

def _resolve_sync(url: str) -> dict:
    # YouTube → use Piped API
    domain = get_domain(url)
    if 'youtube.com' in domain or 'youtu.be' in domain:
        video_id = _extract_youtube_id(url)
        if not video_id:
            raise Exception("Could not extract YouTube video ID")
        return _resolve_youtube_piped(video_id)

    # Everything else → yt-dlp (Twitch, Vimeo, etc.)
    opts = {
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 15,
        'noplaylist': True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if 'entries' in info:
            info = info['entries'][0]
        return {
            'resolved_url': info['url'],
            'audio_url': None,
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