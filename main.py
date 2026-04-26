import os
import json
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path

import cv2
import easyocr
import feedparser
import requests
import yt_dlp
from groq import BadRequestError, Groq

CHANNEL_ID = os.environ.get("CHANNEL_ID", "UC1dHu9GhbHH7RcHKyJdaOvA").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GROQ_FALLBACK_MODELS", "llama-3.3-70b-versatile,llama-3.1-8b-instant"
    ).split(",")
    if m.strip()
]
FRAME_INTERVAL_SECONDS = int(os.environ.get("FRAME_INTERVAL_SECONDS", "3"))
SEEN_FILE = Path("seen_videos.json")

_URL_RE = re.compile(r"https?://\S+|www\.\S+")


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


def fetch_feed() -> list[dict]:
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
    feed = feedparser.parse(rss_url)
    videos = []
    for entry in feed.entries:
        video_id = getattr(entry, "yt_videoid", "") or entry.get("yt_videoid", "")
        if not video_id:
            eid = entry.get("id", "")
            video_id = eid.split(":")[-1] if ":" in eid else ""
        if not video_id:
            continue
        title = entry.get("title", "")
        link = entry.get("link", f"https://www.youtube.com/watch?v={video_id}")
        published = entry.get("published", datetime.now().isoformat())
        videos.append({"id": video_id, "title": title, "link": link, "published": published})
    print(f"RSS 추출 성공 ({len(videos)}개)")
    return videos[:15]


def download_video(video_id: str, output_path: str) -> bool:
    ydl_opts = {
        "format": "worstvideo[ext=mp4]/worstvideo",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    if os.path.exists("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        return True
    except Exception as e:
        print(f"  영상 다운로드 실패: {type(e).__name__}: {e}")
        return False


def extract_text_from_video(video_path: str, reader) -> str:
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    sample_every = max(1, int(fps * FRAME_INTERVAL_SECONDS))
    extracted_texts = []
    last_text = ""
    count = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        if count % sample_every == 0:
            frame_resized = cv2.resize(frame, (640, 360))
            result = reader.readtext(frame_resized, detail=0)
            current_text = " ".join(result).strip()
            current_text = _URL_RE.sub("", current_text).strip()
            if current_text and current_text != last_text and re.search(r"[가-힣]", current_text):
                extracted_texts.append(current_text)
                last_text = current_text
        count += 1
    cap.release()
    print(f"  영상 OCR {len(extracted_texts)}개 블록")
    return "\n".join(extracted_texts)


def summarize(title: str, text: str, client: Groq) -> str:
    if text.strip():
        prompt = (
            "너는 메이플스토리 소식 전달 비서야.\n"
            f"영상 제목: {title}\n"
            "아래는 영상에서 OCR로 추출된 텍스트다.\n"
            "오타/중복이 있을 수 있으니 문맥을 정리해서 다음 형식으로 요약해줘.\n"
            "1. 핵심 요약 (한 줄)\n"
            "2. 상세 내용 (중요 조건/수치 위주 불렛 포인트)\n"
            "3. 이벤트 기간 (없으면 생략)\n"
            "규칙: 한글만 사용. URL 절대 금지. 출처에 없는 내용 추가 금지.\n"
            f"텍스트:\n{text}"
        )
    else:
        prompt = (
            "너는 메이플스토리 소식 전달 비서야.\n"
            f"영상 제목: {title}\n"
            "영상에서 텍스트를 추출하지 못했다. 제목에 명시된 내용만 불렛으로 정리해. 추측이나 창작 금지.\n"
            "한글만 사용. URL 절대 금지."
        )

    models_to_try = [GROQ_MODEL] + [m for m in GROQ_FALLBACK_MODELS if m != GROQ_MODEL]
    last_error = None
    for model_name in models_to_try:
        for attempt in range(3):
            try:
                completion = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                )
                return completion.choices[0].message.content or ""
            except BadRequestError as e:
                if "model_decommissioned" in str(e):
                    last_error = e
                    break
                raise
            except Exception as e:
                if "429" in str(e) or "rate_limit" in str(e).lower():
                    wait = 30 * (attempt + 1)
                    print(f"  Rate limit, {wait}초 대기 후 재시도...")
                    time.sleep(wait)
                    continue
                raise
    raise last_error or RuntimeError("No model available")


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
        "footer": {"text": f"업로드: {published_dt.strftime('%Y-%m-%d')}"},
        "thumbnail": {"url": f"https://img.youtube.com/vi/{video['id']}/hqdefault.jpg"},
    }
    requests.post(webhook_url, json={"embeds": [embed]}, timeout=15).raise_for_status()


def main() -> None:
    seen = load_seen()
    videos = fetch_feed()

    new_videos = [v for v in videos if v["id"] not in seen]
    if not new_videos:
        print("새 영상 없음")
        return

    print(f"새 영상 {len(new_videos)}개 처리 중...")
    reader = easyocr.Reader(["ko", "en"], verbose=False)
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    for video in reversed(new_videos):
        print(f"처리 중: {video['title']}")
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = os.path.join(tmpdir, "video.mp4")
            if download_video(video["id"], video_path) and os.path.exists(video_path):
                text = extract_text_from_video(video_path, reader)
            else:
                text = ""
        summary = summarize(video["title"], text, client)
        send_discord(video, summary)
        seen.add(video["id"])
        print(f"전송 완료: {video['title']}")

    save_seen(seen)


if __name__ == "__main__":
    main()
