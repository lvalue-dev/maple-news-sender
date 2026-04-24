import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

import requests
import yt_dlp
from groq import Groq
from youtube_transcript_api import YouTubeTranscriptApi

CHANNEL_URL = "https://www.youtube.com/channel/UC1dHu9GhbHH7RcHKyJdaOvA/videos"
MONTH_KR = {1:"1월",2:"2월",3:"3월",4:"4월",5:"5월",6:"6월",
            7:"7월",8:"8월",9:"9월",10:"10월",11:"11월",12:"12월"}

_CJK_RE = re.compile(
    r'[぀-ヿ'   # 히라가나 + 가타카나
    r'㐀-䶿'    # CJK 확장 A
    r'一-鿿'    # CJK 통합 한자
    r'豈-﫿]'   # CJK 호환 한자
)


def _strip_cjk(text: str) -> str:
    return _CJK_RE.sub('', text)


def _date_to_iso(upload_date: str) -> str:
    if not upload_date or len(upload_date) < 8:
        return datetime.now().strftime("%Y-%m-%dT00:00:00+00:00")
    return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00+00:00"


def _format_transcript(entries: list) -> str:
    groups: dict[int, list[str]] = {}
    for t in entries:
        bucket = int(t["start"] // 30) * 30
        groups.setdefault(bucket, []).append(t["text"])

    lines = []
    for sec in sorted(groups):
        m, s = divmod(sec, 60)
        lines.append(f"[{m:02d}:{s:02d}] {' '.join(groups[sec])}")
    return "\n".join(lines)


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

    content = ""
    try:
        entries = YouTubeTranscriptApi.get_transcript(video_id, languages=["ko"])
        content = _format_transcript(entries)
    except Exception:
        content = info.get("description", "") or ""

    return {
        "id": video_id,
        "title": info.get("title", ""),
        "link": f"https://www.youtube.com/watch?v={video_id}",
        "published": _date_to_iso(info.get("upload_date", "")),
        "content": content[:15000],
    }


def summarize(video: dict) -> str:
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    prompt = (
        "너는 메이플스토리 뉴스 요약 봇이야.\n"
        "아래 타임스탬프가 포함된 자막을 읽고, 주제가 바뀌는 지점마다 타임라인 항목을 만들어.\n\n"
        "【출력 형식】\n"
        "▶ MM:SS 주제 제목\n"
        "  • 세부 내용 (날짜·아이템명·수량·수치 등 구체적으로)\n"
        "  • 세부 내용\n"
        "\n"
        "▶ MM:SS 주제 제목\n"
        "  • ...\n\n"
        "【규칙】\n"
        "- 한글만 사용. 한자·일본어·중국어 절대 금지\n"
        "- 날짜는 'X월 X일' 형식으로 정확히\n"
        "- 보상 아이템과 수량은 반드시 포함\n"
        "- 자막에 없는 내용은 작성 금지\n"
        "- '있습니다/진행됩니다' 같은 추상 표현 금지\n\n"
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
    published_dt = datetime.fromisoformat(video["published"])

    if len(summary) > 4000:
        summary = summary[:4000] + "\n…(이하 생략)"

    embed = {
        "title": video["title"],
        "url": video["link"],
        "description": summary,
        "color": 0xA020F0,
        "footer": {"text": published_dt.strftime("%Y-%m-%d")},
        "thumbnail": {"url": f"https://img.youtube.com/vi/{video['id']}/hqdefault.jpg"},
    }
    requests.post(webhook_url, json={"embeds": [embed]}, timeout=15).raise_for_status()


def main() -> None:
    print("채널 영상 목록 가져오는 중...")
    videos = fetch_feed()

    if not videos:
        print("영상을 가져오지 못했습니다.")
        sys.exit(1)

    print(f"총 {len(videos)}개 영상 발견")

    by_month: dict[tuple, list] = defaultdict(list)
    for v in sorted(videos, key=lambda x: x["published"]):
        dt = datetime.fromisoformat(v["published"])
        by_month[(dt.year, dt.month)].append(v)

    for (year, month) in sorted(by_month.keys()):
        month_videos = by_month[(year, month)]
        print(f"\n[{year}년 {MONTH_KR[month]}] {len(month_videos)}개")

        send_month_header(month, len(month_videos))
        time.sleep(1)

        for video in month_videos:
            print(f"  상세 정보 가져오는 중: {video['title']}")
            detailed = fetch_video_detail(video["id"])
            print(f"  자막 {len(detailed['content'])}자")
            summary = summarize(detailed)
            send_discord(detailed, summary)
            print(f"  전송 완료")
            time.sleep(1)

    print("\n모든 전송 완료!")


if __name__ == "__main__":
    main()
