import http.cookiejar
import io
import math
import os
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

import requests
import yt_dlp
from groq import Groq
from PIL import Image, ImageEnhance
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound

CHANNEL_URL = "https://www.youtube.com/channel/UC1dHu9GhbHH7RcHKyJdaOvA/videos"
MONTH_KR = {1:"1월",2:"2월",3:"3월",4:"4월",5:"5월",6:"6월",
            7:"7월",8:"8월",9:"9월",10:"10월",11:"11월",12:"12월"}

_CJK_RE = re.compile(
    r'[぀-ヿ'
    r'㐀-䶿'
    r'一-鿿'
    r'豈-﫿]'
)
_URL_RE = re.compile(r'https?://\S+|www\.\S+')


def _strip_cjk(text: str) -> str:
    return _CJK_RE.sub('', text)


def _clean_text(text: str) -> str:
    text = _URL_RE.sub('', text)
    lines = [l.rstrip() for l in text.splitlines()]
    result, prev_blank = [], False
    for l in lines:
        blank = l.strip() == ''
        if blank and prev_blank:
            continue
        result.append(l)
        prev_blank = blank
    return '\n'.join(result).strip()


def _date_to_iso(upload_date: str) -> str:
    if not upload_date or len(upload_date) < 8:
        return datetime.now().strftime("%Y-%m-%dT00:00:00+00:00")
    return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00+00:00"


def _format_transcript(entries: list) -> str:
    groups: dict[int, list[str]] = {}
    for t in entries:
        text = t["text"].strip()
        if len(text) < 4 or not re.search(r'[가-힣a-zA-Z0-9]', text):
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


def _fetch_manual_transcript(video_id: str) -> str:
    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)
        transcript = transcript_list.find_manually_created_transcript(["ko", "ko-KR"])
        entries = transcript.fetch()
        text = _format_transcript(entries)
        if text.strip():
            print(f"  수동 자막 {len(text)}자")
            return text
    except NoTranscriptFound:
        print("  수동 자막 없음 (ASR만 존재 -> 건너뜀)")
    except Exception as e:
        print(f"  자막 API 실패: {type(e).__name__}: {e}")
    return ""


def _scrape_video_info(video_id: str, cookiefile: str = None) -> dict:
    session = requests.Session()
    if cookiefile and os.path.exists(cookiefile):
        jar = http.cookiejar.MozillaCookieJar()
        try:
            jar.load(cookiefile, ignore_discard=True, ignore_expires=True)
            session.cookies = jar
            print(f"  쿠키 {sum(1 for _ in jar)}개 적용")
        except Exception as e:
            print(f"  쿠키 로드 실패: {e}")
    try:
        resp = session.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=15,
        )
        match = re.search(r'ytInitialPlayerResponse\s*=\s*', resp.text)
        if match:
            data, _ = json.JSONDecoder().raw_decode(resp.text, match.end())
            play_status = data.get("playabilityStatus", {}).get("status", "")
            desc = data.get("videoDetails", {}).get("shortDescription", "")
            spec = (data.get("storyboards", {})
                        .get("playerStoryboardSpecRenderer", {})
                        .get("spec", ""))
            print(f"  페이지 스크래핑 status={play_status} desc={len(desc)}자 spec={'있음' if spec else '없음'}")
            if play_status == "OK":
                return {"description": desc, "storyboard_spec": spec}
            else:
                print(f"  -> 접근 제한 (쿠키 필요 또는 만료)")
        else:
            print(f"  페이지 스크래핑: ytInitialPlayerResponse 없음 (HTTP {resp.status_code})")
    except Exception as e:
        print(f"  페이지 스크래핑 실패: {type(e).__name__}: {e}")
    return {"description": "", "storyboard_spec": ""}


def _fetch_video_details_ytdlp(video_id: str, cookiefile: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "cookiefile": cookiefile,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False,
            )
        desc = info.get("description", "") or ""
        chapters = info.get("chapters") or []
        sub_urls = _extract_sub_urls(info)
        print(f"  yt-dlp 개별 추출 desc={len(desc)}자 chapters={len(chapters)} subs={len(sub_urls)}")
        return {"description": desc, "chapters": chapters, "subtitle_urls": sub_urls}
    except Exception as e:
        print(f"  yt-dlp 개별 추출 실패: {type(e).__name__}: {e}")
        return {}


def _parse_storyboard(spec: str) -> list[dict]:
    if not spec:
        return []
    parts = spec.split("|")
    base_url = parts[0]
    levels = []
    for level_idx, params_str in enumerate(parts[1:]):
        p = params_str.split("#")
        if len(p) < 6:
            continue
        try:
            w, h, count, interval_ms, cols, rows = (
                int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5])
            )
        except ValueError:
            continue
        sigh = p[6] if len(p) > 6 else ""
        sheet_url_base = base_url.replace("$L", str(level_idx)).replace("$N", sigh)
        sheets_needed = math.ceil(count / (cols * rows))
        levels.append({
            "width": w, "height": h, "count": count,
            "interval_ms": interval_ms, "cols": cols, "rows": rows,
            "sheet_url_base": sheet_url_base,
            "sheets_needed": sheets_needed,
        })
    return sorted(levels, key=lambda x: x["width"], reverse=True)


def _run_easyocr(img: Image.Image, reader) -> str:
    result = reader.readtext(img, detail=0, paragraph=True,
                             text_threshold=0.5, low_text=0.3)
    text = " ".join(result).strip()
    text = _strip_cjk(text)
    text = _URL_RE.sub("", text).strip()
    return text if re.search(r'[가-힣]', text) else ""


def _ocr_thumbnail(video_id: str, reader) -> str:
    for quality in ("maxresdefault", "sddefault", "hqdefault"):
        url = f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"
        try:
            img_bytes = requests.get(url, timeout=10).content
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            if img.width < 300:
                continue
            img = ImageEnhance.Contrast(img).enhance(1.3)
            text = _run_easyocr(img, reader)
            if text:
                print(f"  썸네일 OCR ({quality}) {len(text)}자")
                return text
        except Exception:
            continue
    return ""


def _ocr_storyboard(spec: str, reader) -> str:
    levels = _parse_storyboard(spec)
    if not levels:
        return ""
    best = next((l for l in levels if l["width"] >= 240), None)
    if not best:
        return ""

    interval_sec = best["interval_ms"] / 1000
    cols, rows = best["cols"], best["rows"]
    frame_w, frame_h = best["width"], best["height"]
    seen_texts: set[str] = set()
    results: list[tuple[float, str]] = []
    frame_global_idx = 0

    for sheet_idx in range(best["sheets_needed"]):
        url = best["sheet_url_base"] + f"M{sheet_idx}.jpg"
        try:
            img_bytes = requests.get(url, timeout=10).content
            sheet = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        except Exception:
            frame_global_idx += cols * rows
            continue

        for r in range(rows):
            for c in range(cols):
                if frame_global_idx >= best["count"]:
                    break
                timestamp = frame_global_idx * interval_sec
                frame = sheet.crop((
                    c * frame_w, r * frame_h,
                    (c + 1) * frame_w, (r + 1) * frame_h,
                ))
                frame = frame.resize((frame_w * 4, frame_h * 4), Image.LANCZOS)
                frame = ImageEnhance.Contrast(frame).enhance(1.5)
                text = _run_easyocr(frame, reader)
                if text and text not in seen_texts:
                    seen_texts.add(text)
                    m, s = divmod(int(timestamp), 60)
                    results.append((timestamp, f"[{m:02d}:{s:02d}] {text}"))
                frame_global_idx += 1

    if not results:
        return ""
    results.sort(key=lambda x: x[0])
    print(f"  스토리보드 OCR {len(results)}개 블록")
    return "\n".join(t for _, t in results)


def _ocr_all(video_id: str, storyboard_spec: str) -> str:
    try:
        import easyocr
        reader = easyocr.Reader(["ko", "en"], verbose=False)
    except Exception as e:
        print(f"  EasyOCR 초기화 실패: {e}")
        return ""
    thumb_text = _ocr_thumbnail(video_id, reader)
    sb_text = _ocr_storyboard(storyboard_spec, reader)
    parts = []
    if thumb_text:
        parts.append(f"[썸네일] {thumb_text}")
    if sb_text:
        parts.append(sb_text)
    return "\n".join(parts)


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
        desc = entry.get("description", "") or ""
        print(f"  [{entry.get('title', '')[:25]}] desc={len(desc)}자 chapters={len(chapters)} subs={len(sub_urls)}")
        videos.append({
            "id": entry["id"],
            "title": entry.get("title", ""),
            "link": f"https://www.youtube.com/watch?v={entry['id']}",
            "published": _date_to_iso(entry.get("upload_date", "")),
            "description": desc,
            "chapters": chapters,
            "subtitle_urls": sub_urls,
            "storyboard_spec": "",
        })
    return videos


def fetch_transcript(video: dict) -> dict:
    cookiefile = "cookies.txt" if os.path.exists("cookies.txt") else None
    parts = []

    desc = video.get("description", "").strip()
    storyboard_spec = video.get("storyboard_spec", "")
    chapters = list(video.get("chapters") or [])
    sub_urls = list(video.get("subtitle_urls") or [])

    if len(desc) < 100 or not storyboard_spec:
        info = _scrape_video_info(video["id"], cookiefile)
        if len(info.get("description", "")) > len(desc):
            desc = info["description"]
        if not storyboard_spec:
            storyboard_spec = info.get("storyboard_spec", "")

    if len(desc) < 100 and cookiefile:
        detail = _fetch_video_details_ytdlp(video["id"], cookiefile)
        if detail:
            if len(detail.get("description", "")) > len(desc):
                desc = detail["description"]
            if not chapters:
                chapters = detail.get("chapters", [])
            if not sub_urls:
                sub_urls = detail.get("subtitle_urls", [])

    cleaned_desc = _clean_text(desc)
    if cleaned_desc:
        parts.append("【영상 설명】\n" + cleaned_desc)

    manual_text = _fetch_manual_transcript(video["id"])
    if manual_text:
        parts.append("【수동 자막】\n" + manual_text)

    sub_text = _fetch_subtitle_content(sub_urls, cookiefile)
    if sub_text:
        parts.append("【자막】\n" + sub_text)
        print(f"  자막 URL {len(sub_text)}자")

    chapters_text = _format_chapters(chapters)
    if chapters_text:
        parts.append("【챕터】\n" + chapters_text)
        print(f"  챕터 {len(chapters)}개 사용")
    else:
        desc_ts = _parse_desc_timestamps(desc)
        if desc_ts:
            parts.append("【설명 타임스탬프】\n" + desc_ts)
            print(f"  설명 타임스탬프 {len(desc_ts)}자")

    ocr_text = _ocr_all(video["id"], storyboard_spec)
    if ocr_text:
        parts.append("【화면 텍스트(OCR)】\n" + ocr_text)

    content = "\n\n".join(parts)
    print(f"  최종 콘텐츠 {len(content)}자")
    return {**video, "content": content[:15000]}


def summarize(video: dict) -> str:
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    content = video.get("content", "").strip()

    has_real_content = any(tag in content for tag in (
        "【수동 자막】", "【자막】", "【챕터】",
        "【화면 텍스트(OCR)】", "【설명 타임스탬프】",
    )) or len(content) > 300

    if has_real_content:
        prompt = (
            "너는 메이플스토리 뉴스 요약 봇이야.\n"
            "아래 영상 정보를 읽고 핵심 내용을 한국어로 요약해.\n\n"
            "출력 형식:\n"
            "타임스탬프가 있으면 -> 해당 시간대 주제 제목을 굵게\n"
            "타임스탬프가 없으면 -> 주제 제목을 굵게\n"
            "각 항목 아래에 세부 내용을 불렛으로 나열\n\n"
            "규칙:\n"
            "- 한글만 사용. 한자·일본어·중국어 절대 금지\n"
            "- 날짜는 'X월 X일' 형식으로 정확히\n"
            "- 보상 아이템·수량은 반드시 포함\n"
            "- 출처에 없는 내용 작성 금지\n"
            "- 추상적 표현 금지\n"
            "- URL 포함 금지\n"
            "- 내용이 없는 항목은 생략\n\n"
            f"제목: {video['title']}\n\n"
            f"{content}\n"
        )
    else:
        prompt = (
            "너는 메이플스토리 뉴스 요약 봇이야.\n"
            "아래 영상 제목에 핵심 정보가 담겨 있다.\n"
            "제목에 명시된 내용만 불렛으로 정리해. 추측이나 창작 금지.\n"
            "한글만 사용. URL 절대 금지.\n\n"
            f"제목: {video['title']}\n"
        )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.choices[0].message.content.strip()
            result = _strip_cjk(result)
            result = _URL_RE.sub('', result).strip()
            return result
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
            "title": f"2026년 {MONTH_KR[month]} - 영상 {count}개",
            "color": 0x5865F2,
        }]
    }
    requests.post(webhook_url, json=payload, timeout=15).raise_for_status()


def send_discord(video: dict, summary: str) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    published_dt = datetime.fromisoformat(video["published"])

    if len(summary) > 4000:
        summary = summary[:4000] + "\n...(이하 생략)"

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
