import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime

import requests
from google import genai

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=UC1dHu9GhbHH7RcHKyJdaOvA"
NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
MONTH_KR = {1:"1월",2:"2월",3:"3월",4:"4월",5:"5월",6:"6월",
            7:"7월",8:"8월",9:"9월",10:"10월",11:"11월",12:"12월"}


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


def send_month_header(month: int, count: int) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    payload = {
        "embeds": [{
            "title": f"📅 2026년 {MONTH_KR[month]} — 영상 {count}개",
            "color": 0x5865F2,
        }]
    }
    requests.post(webhook_url, json=payload, timeout=15).raise_for_status()


def send_discord(video: dict, summary: str) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    published_dt = datetime.fromisoformat(video["published"].replace("Z", "+00:00"))

    embed = {
        "title": video["title"],
        "url": video["link"],
        "description": summary,
        "color": 0xA020F0,
        "footer": {"text": published_dt.strftime("%Y-%m-%d %H:%M UTC")},
        "thumbnail": {"url": f"https://img.youtube.com/vi/{video['id']}/hqdefault.jpg"},
    }
    requests.post(webhook_url, json={"embeds": [embed]}, timeout=15).raise_for_status()


def main() -> None:
    print("RSS 피드 가져오는 중...")
    videos = fetch_feed()

    videos_2026 = []
    for v in videos:
        dt = datetime.fromisoformat(v["published"].replace("Z", "+00:00"))
        if dt.year == 2026:
            v["month"] = dt.month
            videos_2026.append(v)

    if not videos_2026:
        print("RSS 피드에 2026년 영상이 없습니다. (RSS는 최신 15개만 제공)")
        sys.exit(0)

    by_month: dict[int, list] = defaultdict(list)
    for v in sorted(videos_2026, key=lambda x: x["published"]):
        by_month[v["month"]].append(v)

    print(f"2026년 영상 {len(videos_2026)}개 발견 ({len(by_month)}개월치)")

    for month in sorted(by_month.keys()):
        month_videos = by_month[month]
        print(f"\n[2026년 {MONTH_KR[month]}] {len(month_videos)}개")

        send_month_header(month, len(month_videos))
        time.sleep(1)

        for video in month_videos:
            print(f"  요약 중: {video['title']}")
            summary = summarize(video)
            send_discord(video, summary)
            print(f"  전송 완료")
            time.sleep(1)

    print("\n모든 전송 완료!")


if __name__ == "__main__":
    main()
