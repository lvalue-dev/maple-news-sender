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
        text = t["text"].strip()
        if len(text) < 4:
            continue
        bucket = int(t["start"] // 30) * 30
        groups.setdefault(bucket, []).append(text)

    lines = []
    for sec in sorted(groups):
        m, s = divmod(sec, 60)
        lines.append(f"[{m:02d}:{s:02d}] {' '.join(groups[sec])}")
    return "\n".join(lines)


def _format_chapters(chapters: list) -> str:
    lines = []
    for ch in chapters or []:
        start = int(ch.get("start_time", 0))
        end = ch.get("end_time")
        title = ch.get("title", "").strip()
        if not title or title.startswith("<Untitled"):
            continue
        m_s, s_s = divmod(start, 60)
        suffix = f"~{int(end) // 60:02d}:{int(end) % 60:02d}" if end else ""
        lines.append(f"[{m_s:02d}:{s_s:02d}{suffix}] {title}")
    return "\n".join(lines)


def _parse_desc_timestamps(description: str) -> str:
    if not description:
        return ""
    ts_re = re.compile(
        r'^(\d{1,2}:\d{2}(?::\d{2})?)\s+(.+)$'
        r'|^(.+?)\s+(\d{1,2}:\d{2}(?::\d{2})?)$',
        re.MULTILINE
    )
    lines = []
    for m in ts_re.finditer(description):
        if m.group(1):
            ts, title = m.group(1), m.group(2).strip()
        else:
            ts, title = m.group(4), m.group(3).strip()
        if title:
            lines.append(f"[{ts}] {title}")
    return "\n".join(lines)


def _extract_sub_urls(entry: dict) -> list:
    subs = entry.get("subtitles") or {}
    auto = entry.get("automatic_captions") or {}
    for lang in ("ko", "ko-KR"):
        if lang in subs:
            return subs[lang]
    for lang in ("ko", "a.ko", "a-ko"):
        if lang in auto:
            return auto[lang]
    return []


def _parse_vtt(vtt_text: str) -> str:
    entries = []
    pattern = re.compile(
        r'(\d+:\d+:\d+\.\d+|\d+:\d+\.\d+)\s*-->.*\n((?:(?!-->).+\n?)*)',
        re.MULTILINE
    )
    for m in pattern.finditer(vtt_text):
        ts = m.group(1)
        parts = ts.split(":")
        start = (
            float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            if len(parts) == 3
            else float(parts[0]) * 60 + float(parts[1])
        )
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if text and len(text) >= 4:
            entries.append({"start": start, "text": text})
    return _format_transcript(entries)


def _fetch_subtitle_content(sub_urls: list, cookiefile: str = None) -> str:
    if not sub_urls:
        return ""
    url_entry = next((s for s in sub_urls if s.get("ext") == "vtt"), sub_urls[0])
    url = url_entry.get("url", "")
    if not url:
        return ""
    ydl_opts = {"quiet": True, "no_warnings": True}
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile
    try:
        from yt_dlp.networking.common import Request
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.urlopen(Request(url)).read().decode("utf-8")
            return _parse_vtt(data)
    except Exception as e:
        print(f"  자막 URL 실패: {type(e).__name__}")
        return ""


def fetch_feed() -> list[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "playlistend": 15,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(CHANNEL_URL, download=False)
        entries = info.get("entries") or []
        print(f"full 추출 성공 ({len(entries)}개)")
    except Exception as e:
        print(f"full 추출 실패 ({e}), flat으로 재시도...")
        flat_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlistend": 15,
        }
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            info = ydl.extract_info(CHANNEL_URL, download=False)
        entries = info.get("entries") or []

    videos = []
    for entry in entries:
        if not entry:
            continue
        chapters = entry.get("chapters") or []
        sub_urls = _extract_sub_urls(entry)
        if chapters:
            print(f"  [{entry.get('title', '')[:20]}] 챕터 {len(chapters)}개")
        if sub_urls:
            print(f"  [{entry.get('title', '')[:20]}] 자막 URL {len(sub_urls)}개")
        videos.append({
            "id": entry["id"],
            "title": entry.get("title", ""),
            "link": f"https://www.youtube.com/watch?v={entry['id']}",
            "published": _date_to_iso(entry.get("upload_date", "")),
            "description": entry.get("description", "") or "",
            "chapters": chapters,
            "subtitle_urls": sub_urls,
        })
    return videos


def fetch_transcript(video: dict) -> dict:
    parts = []

    if video.get("description", "").strip():
        parts.append("【영상 설명】\n" + video["description"].strip())

    # 우선순위 1: yt_dlp 챕터 마커
    chapters_text = _format_chapters(video.get("chapters", []))
    if chapters_text:
        parts.append("【챕터】\n" + chapters_text)
        print(f"  챕터 {len(video.get('chapters', []))}개 사용")
    else:
        # 우선순위 2: 설명 타임스탬프 파싱
        desc_ts = _parse_desc_timestamps(video.get("description", ""))
        if desc_ts:
            parts.append("【설명 타임스탬프】\n" + desc_ts)
            print(f"  설명 타임스탬프 {len(desc_ts)}자")

    # 우선순위 3: 자막 URL 다운로드 (yt_dlp full 추출에서 얻은 URL)
    cookiefile = "cookies.txt" if os.path.exists("cookies.txt") else None
    sub_text = _fetch_subtitle_content(video.get("subtitle_urls", []), cookiefile)
    if sub_text:
        parts.append("【자막】\n" + sub_text)
        print(f"  자막 URL {len(sub_text)}자")
    else:
        # 우선순위 4: youtube_transcript_api fallback
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video["id"])
            transcript = None
            for lang in (["ko", "ko-KR"], ["a.ko"]):
                try:
                    transcript = transcript_list.find_transcript(lang)
                    break
                except Exception:
                    continue
            if transcript is None:
                transcript = next(iter(transcript_list))
            entries = transcript.fetch()
            t_text = _format_transcript(entries)
            if t_text.strip():
                parts.append("【자막】\n" + t_text.strip())
                print(f"  자막 API {len(t_text)}자 ({transcript.language_code})")
        except Exception as e:
            print(f"  자막 없음: {type(e).__name__}")

    content = "\n\n".join(parts)
    print(f"  최종 콘텐츠 {len(content)}자")
    return {**video, "content": content[:15000]}


def summarize(video: dict) -> str:
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    has_content = bool(video.get("content", "").strip())

    if has_content:
        prompt = (
            "너는 메이플스토리 뉴스 요약 봇이야.\n"
            "아래 영상 정보를 읽고, 주제가 바뀌는 지점마다 타임라인 항목을 만들어.\n\n"
            "【출력 형식】\n"
            "▶ MM:SS 주제 제목  (타임스탬프 없으면 ▶ 주제 제목)\n"
            "  • 세부 내용 (날짜·아이템명·수량·수치 등 구체적으로)\n"
            "  • ...\n\n"
            "【규칙】\n"
            "- 한글만 사용. 한자·일본어·중국어 절대 금지\n"
            "- 날짜는 'X월 X일' 형식으로 정확히\n"
            "- 보상 아이템·수량 반드시 포함\n"
            "- 정보 출처에 없는 내용 작성 금지\n"
            "- '있습니다/진행됩니다' 같은 추상 표현 금지\n\n"
            f"제목: {video['title']}\n\n"
            f"{video['content']}\n"
        )
    else:
        prompt = (
            "너는 메이플스토리 뉴스 요약 봇이야.\n"
            "자막을 구하지 못해 제목만으로 요약해야 해.\n"
            "제목에서 유추할 수 있는 내용을 한국어로 3~5줄로 작성해.\n"
            "한글만 사용. 한자·일본어·중국어 절대 금지.\n\n"
            f"제목: {video['title']}\n"
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
            print(f"처리 중: {video['title']}")
            detailed = fetch_transcript(video)
            summary = summarize(detailed)
            send_discord(detailed, summary)
            print(f"  전송 완료")
            time.sleep(1)

    print("\n모든 전송 완료!")


if __name__ == "__main__":
    main()
