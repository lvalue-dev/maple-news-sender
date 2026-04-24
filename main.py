import os
import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
import yt_dlp
from groq import Groq
from youtube_transcript_api import YouTubeTranscriptApi

CHANNEL_URL = "https://www.youtube.com/channel/UC1dHu9GhbHH7RcHKyJdaOvA/videos"
SEEN_FILE = Path("seen_videos.json")

# 히라가나/가타카나/한자 유니코드 범위 (한글 AC00-D7AF는 포함 안 됨)
_CJK_RE = re.compile(
    r'[぀-ヿ'   # 히라가나 + 가타카나
    r'㐀-䶿'    # CJK 확장 A
    r'一-鿿'    # CJK 통합 한자
    r'豈-﫿]'   # CJK 호환 한자
)


def _strip_cjk(text: str) -> str:
    return _CJK_RE.sub('', text)


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
        })
    return videos


def fetch_video_detail(video_id: str) -> dict:
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)

    transcript = ""
    try:
        entries = YouTubeTranscriptApi.get_transcript(video_id, languages=["ko"])
        transcript = " ".join(t["text"] for t in entries)
    except Exception:
        transcript = info.get("description", "") or ""

    return {
        "id": video_id,
        "title": info.get("title", ""),
        "link": f"https://www.youtube.com/watch?v={video_id}",
        "published": _date_to_iso(info.get("upload_date", "")),
        "content": transcript[:10000],
    }


def summarize(video: dict) -> str:
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    prompt = (
        "너는 메이플스토리 뉴스 요약 봇이야. 아래 영상 자막을 읽고 항목별로 한국어로 작성해.\n\n"
        "【출력 형식 - 반드시 이 형식 그대로】\n"
        "**유형**: 이벤트공략 / 업데이트 / 보스공략 / 기타 중 하나\n"
        "**핵심 요약**: (한 줄)\n"
        "**주요 내용**:\n"
        "- (구체적 항목. 날짜·수량·아이템명 등 숫자와 고유명사 반드시 포함)\n"
        "- ...\n"
        "**이벤트 기간**: (예: 4월 24일 ~ 5월 14일 / 언급 없으면 '언급 없음')\n"
        "**보상**: (구체적 아이템명과 수량 / 없으면 '언급 없음')\n"
        "**주의사항**: (있으면 기재 / 없으면 생략)\n\n"
        "【규칙】\n"
        "- 한글만 사용. 한자·일본어·중국어 문자 절대 금지\n"
        "- '있습니다', '진행됩니다' 같은 추상 표현 금지. 실제 내용 서술\n"
        "- 자막에 없는 내용은 '언급 없음'\n\n"
        f"제목: {video['title']}\n\n"
        f"자막:\n{video['content'] or '(자막 없음)'}\n"
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.choices[0].message.content.strip()
            return _strip_cjk(result)
        except Exception as e:
            if "429" in str(e) or "rate_limit" in str(e).lower():
                wait = 30 * (attempt + 1)
                print(f"  Rate limit, {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Groq API 재시도 초과")


def send_discord(video: dict, summary: str) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    published_dt = datetime.fromisoformat(video["published"])

    embed = {
        "title": video["title"],
        "url": video["link"],
        "description": summary,
        "color": 0xA020F0,
        "footer": {"text": f"업로드: {published_dt.strftime('%Y-%m-%d')}"},
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
        detailed = fetch_video_detail(video["id"])
        summary = summarize(detailed)
        send_discord(detailed, summary)
        seen.add(video["id"])
        print(f"전송 완료: {video['title']}")

    save_seen(seen)


if __name__ == "__main__":
    main()
