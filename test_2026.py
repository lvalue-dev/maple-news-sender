import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime

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

MONTH_KR = {
    1: "1월", 2: "2월", 3: "3월", 4: "4월", 5: "5월", 6: "6월",
    7: "7월", 8: "8월", 9: "9월", 10: "10월", 11: "11월", 12: "12월",
}

_URL_RE = re.compile(r"https?://\S+|www\.\S+")


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
    return videos


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
            f"영상 제목: {title}\n\n"
            "아래는 영상 화면에서 OCR로 추출한 텍스트다. 오타·띄어쓰기 오류가 있을 수 있다.\n"
            "텍스트를 꼼꼼히 읽고 아래 형식으로 요약해줘.\n\n"
            "【형식】\n"
            "**핵심 요약**: 한 줄로\n"
            "**상세 내용**:\n"
            "- 스킬·아이템·직업·몬스터 이름은 반드시 원문 그대로 표기\n"
            "- 수치 변경은 '이전값 → 이후값' 형식으로 (예: 데미지 300% → 350%)\n"
            "- 조건·제한·획득 방법 등 구체적인 조건 포함\n"
            "- 보상 아이템과 수량 포함\n"
            "- 각 항목별로 불렛 하나씩, 내용이 많으면 세부 항목 들여쓰기 사용\n"
            "**이벤트 기간**: X월 X일 ~ X월 X일 (없으면 생략)\n\n"
            "규칙:\n"
            "- 한글만 사용. 한자·일본어·중국어 절대 금지\n"
            "- URL 포함 금지\n"
            "- 텍스트에 없는 내용 추가 금지. 추측·창작 금지\n"
            "- 내용이 적더라도 텍스트에 있는 것은 빠짐없이 적어\n\n"
            f"OCR 텍스트:\n{text}"
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

    reader = easyocr.Reader(["ko", "en"], verbose=False)
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

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
            with tempfile.TemporaryDirectory() as tmpdir:
                video_path = os.path.join(tmpdir, "video.mp4")
                if download_video(video["id"], video_path) and os.path.exists(video_path):
                    text = extract_text_from_video(video_path, reader)
                else:
                    text = ""
            summary = summarize(video["title"], text, client)
            send_discord(video, summary)
            print(f"  전송 완료")
            time.sleep(1)

    print("\n모든 전송 완료!")


if __name__ == "__main__":
    main()
