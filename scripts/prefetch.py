#!/usr/bin/env python3
"""
Add a video to the curated list.

Usage:
    python scripts/prefetch.py <youtube_url>

Run this locally whenever you want to add a new video.
It fetches subtitles, translates, resolves emojis/images,
and saves everything to data/ so the deployed app needs no
YouTube access at runtime.
"""

import sys, os, json, asyncio, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import yt_dlp
import httpx
import nltk
import emoji as emoji_lib
from deep_translator import GoogleTranslator

nltk.download("stopwords", quiet=True)
nltk.download("wordnet", quiet=True)

# Import shared logic from main.py
from main import (
    emoji_lookup, get_candidates, assess_level, find_emoji,
    CONCRETE_WORDS, extract_video_id,
)

# Populate emoji_lookup (normally done in FastAPI lifespan)
for _char, _data in emoji_lib.EMOJI_DATA.items():
    _name = _data.get("en", "").strip(":").lower().replace("_", " ").strip()
    if _name:
        emoji_lookup.setdefault(_name, _char)
    for _alias in _data.get("alias", []):
        _clean = _alias.strip(":").replace("_", " ").lower().strip()
        if _clean:
            emoji_lookup.setdefault(_clean, _char)

PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(DATA_DIR, exist_ok=True)

SPANISH_LANGS = ["es", "es-orig", "es-419", "es-ES", "es-MX", "es-AR", "es-CO", "es-US"]


async def prefetch_video(url: str, level_override: str | None = None):
    vid = extract_video_id(url)
    print(f"\nPrefetching {vid} …")

    # ── 1. Fetch video info + subtitles via yt-dlp ────────────────────────────
    ydl_opts = {
        "skip_download": True, "quiet": True, "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title", "")
    duration = info.get("duration", 0)
    print(f"  Title   : {title}")
    print(f"  Duration: {duration // 60}:{duration % 60:02d}")

    # ── 2. Check embeddable ───────────────────────────────────────────────────
    try:
        urllib.request.urlopen(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json",
            timeout=5,
        )
        embeddable = True
    except Exception:
        embeddable = False
        print("  WARNING: video may not be embeddable on external sites")

    # ── 3. Get Spanish subtitle URL ───────────────────────────────────────────
    all_subs = {**info.get("automatic_captions", {}), **info.get("subtitles", {})}
    sub_url = None
    for lang in SPANISH_LANGS:
        for fmt in all_subs.get(lang, []):
            if fmt.get("ext") == "json3":
                sub_url = fmt["url"]
                break
        if sub_url:
            break

    if not sub_url:
        print(f"  ERROR: no Spanish subtitles found. Available: {list(all_subs.keys())}")
        sys.exit(1)

    async with httpx.AsyncClient() as client:
        r = await client.get(sub_url, timeout=15.0)
        r.raise_for_status()
        sub_data = r.json()

    transcript = []
    for ev in sub_data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs", [])).strip()
        if text and text != "\n":
            transcript.append({
                "text": text,
                "start": ev["tStartMs"] / 1000,
                "duration": ev.get("dDurationMs", 2000) / 1000,
            })
    print(f"  Subtitle segments: {len(transcript)}")

    # ── 4. Level + candidates ─────────────────────────────────────────────────
    level = level_override or assess_level(transcript)
    candidates = get_candidates(transcript)
    print(f"  Level: {level}{' (override)' if level_override else ''}  |  Candidates: {len(candidates)}")

    # ── 5. Translate ──────────────────────────────────────────────────────────
    translator = GoogleTranslator(source="es", target="en")
    unique_words = list(dict.fromkeys(c["word"] for c in candidates if not c["word"].isdigit()))
    sem = asyncio.Semaphore(15)
    loop = asyncio.get_event_loop()

    async def translate_one(word):
        async with sem:
            try:
                result = await loop.run_in_executor(None, translator.translate, word)
                return (result or word).lower().strip()
            except Exception:
                return word

    translations = await asyncio.gather(*[translate_one(w) for w in unique_words])
    translation_map = dict(zip(unique_words, translations))
    print(f"  Translated {len(translation_map)} words")

    # ── 6. Enrich: emoji / number ─────────────────────────────────────────────
    enriched = []
    for c in candidates:
        word = c["word"]
        eng = word if word.isdigit() else translation_map.get(word, word)
        if eng.isdigit() and int(eng) > 10:
            enriched.append({**c, "search_term": eng, "number": eng})
            continue
        search = eng if eng in CONCRETE_WORDS else next(
            (w for w in eng.split() if w in CONCRETE_WORDS), None
        )
        if search:
            entry = {**c, "search_term": search}
            char = find_emoji(search)
            if char:
                entry["emoji"] = char
            enriched.append(entry)

    needs_image = [e for e in enriched if "emoji" not in e and "number" not in e]
    print(f"  Emojis resolved: {len(enriched) - len(needs_image)}  |  Need image: {len(needs_image)}")

    # ── 7. Pre-fetch images ───────────────────────────────────────────────────
    if needs_image:
        img_sem = asyncio.Semaphore(5)
        done = 0

        async def fetch_image(entry):
            nonlocal done
            async with img_sem:
                if PIXABAY_API_KEY:
                    try:
                        async with httpx.AsyncClient() as c:
                            r = await c.get(
                                "https://pixabay.com/api/",
                                params={
                                    "key": PIXABAY_API_KEY,
                                    "q": entry["search_term"],
                                    "image_type": "illustration",
                                    "per_page": 3,
                                    "safesearch": "true",
                                },
                                timeout=8.0,
                            )
                            hits = r.json().get("hits", [])
                            if hits:
                                done += 1
                                return {**entry, "image_url": hits[0]["webformatURL"]}
                    except Exception:
                        pass
                return entry

        image_results = await asyncio.gather(*[fetch_image(e) for e in needs_image])
        image_map = {e["time"]: e for e in image_results}
        enriched = [image_map.get(e["time"], e) for e in enriched]
        print(f"  Images fetched: {done}")

    # ── 8. Save ───────────────────────────────────────────────────────────────
    video_data = {
        "video_id": vid,
        "title": title,
        "duration": duration,
        "level": level,
        "word_events": enriched,
    }
    out_path = os.path.join(DATA_DIR, f"{vid}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(video_data, f, ensure_ascii=False)
    print(f"  Saved: {out_path}")

    # ── 9. Update index ───────────────────────────────────────────────────────
    index_path = os.path.join(DATA_DIR, "videos.json")
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            videos = json.load(f)
    else:
        videos = []

    videos = [v for v in videos if v["video_id"] != vid]
    videos.append({
        "video_id": vid,
        "title": title,
        "duration": duration,
        "level": level,
        "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
        "embeddable": embeddable,
    })

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(videos, f, indent=2, ensure_ascii=False)
    print(f"  Updated videos.json ({len(videos)} total)")
    print("  Done!")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="YouTube URL")
    parser.add_argument("--level", choices=["Super Beginner", "Beginner", "Intermediate", "Advanced"],
                        help="Override the auto-detected level")
    args = parser.parse_args()
    asyncio.run(prefetch_video(args.url, level_override=args.level))
