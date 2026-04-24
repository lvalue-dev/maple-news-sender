import os
import json
import time
from datetime import datetime
from pathlib import Path

import requests
import yt_dlp
from google import genai

CHANNEL_URL = "https://www.youtube.com/channel/UC1dHu9GhbHH7RcHKyJdaOvA/videos"
SEEN_FILE = Path("seen_videos.json")


def _date_to_iso(upload_date: str) -> str:
    if not upload_date or len(upload_date) < 8:
        return datetime.now().strftime("%Y-%m-%dT00:00:00+00:00")
    return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00+00:00"


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


def fetch_feed() -> list[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 15,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(CHANNEL_URL, download=False)

    videos = []
    for entry in (info.get("entries") or []):
        videos.append({
            "id": entry["id"],
            "title": entry.get("title", ""),
            "link": f"https://www.youtube.com/watch?v={entry['id']}",
            "published": _date_to_iso(entry.get("upload_date", "")),
            "description": entry.get("description", "") or "",
        })
    return videos


def summarize(video: dict) -> str:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    prompt = (
        "당신은 메이플스토리 게임 뉴스 요약 봇입니다.\n"
        "아래 YouTube 영상 정보를 바탕으로 핵심 내용을 한국어로 3~5줄 이내로 간결하게 요약해주세요.\n"
        "불필요한 인사말, 추가 설명 없이 요약 내용만 출력하세요.\n\n"
        f"제목: {video['title']}\n"
        f"설명:\n{video['description'] or '(설명 없음)'}\n"
    )

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            return response.text.strip()
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = 30 * (attempt + 1)
                print(f"  Rate limit, {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini API 재시도 초과")


def send_discord(video: dict, summary: str) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]

    published_dt = datetime.fromisoformat(video["published"])
    published_str = published_dt.strftime("%Y-%m-%d")

    embed = {
        "title": video["title"],
        "url": video["link"],
        "description": summary,
        "color": 0xA020F0,
        "footer": {"text": f"업로드: {published_str}"},
        "thumbnail": {
            "url": f"https://img.youtube.com/vi/{video['id']}/hqdefault.jpg"
        },
    }

    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    resp.raise_for_status()


def main() -> None:
    seen = load_seen()
    videos = fetch_feed()

    new_videos = [v for v in videos if v["id"] not in seen]

    if not new_videos:
        print("새 영상 없음")
        return

    for video in reversed(new_videos):
        print(f"처리 중: {video['title']}")
        summary = summarize(video)
        send_discord(video, summary)
        seen.add(video["id"])
        print(f"전송 완료: {video['title']}")

    save_seen(seen)


if __name__ == "__main__":
    main()
