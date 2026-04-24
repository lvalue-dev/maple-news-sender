import os
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests
from google import genai

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=UC1dHu9GhbHH7RcHKyJdaOvA"
SEEN_FILE = Path("seen_videos.json")
NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


def fetch_feed() -> list[dict]:
    resp = requests.get(RSS_URL, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    videos = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.find("yt:videoId", NS).text
        title = entry.find("atom:title", NS).text
        link = entry.find("atom:link", NS).get("href")
        published = entry.find("atom:published", NS).text
        description = ""
        media_group = entry.find("{http://search.yahoo.com/mrss/}group")
        if media_group is not None:
            media_desc = media_group.find("{http://search.yahoo.com/mrss/}description")
            if media_desc is not None and media_desc.text:
                description = media_desc.text.strip()

        videos.append({
            "id": video_id,
            "title": title,
            "link": link,
            "published": published,
            "description": description,
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

    published_dt = datetime.fromisoformat(video["published"].replace("Z", "+00:00"))
    published_str = published_dt.strftime("%Y-%m-%d %H:%M UTC")

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

    payload = {"embeds": [embed]}
    resp = requests.post(webhook_url, json=payload, timeout=15)
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
