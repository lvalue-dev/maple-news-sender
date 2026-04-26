import atexit
import base64
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
_COOKIE_TMPFILE: str | None = None


def setup_cookies() -> str | None:
    global _COOKIE_TMPFILE

    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if b64:
        try:
            data = base64.b64decode(b64)
            fd, path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            _COOKIE_TMPFILE = path
            atexit.register(cleanup_cookies)
            print(f"[쿠키] YOUTUBE_COOKIES_B64 → 임시 파일 복원 ({len(data):,} bytes): {path}")
            return path
        except Exception as e:
            print(f"[쿠키] base64 복원 실패: {e}")

    explicit = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    if explicit and os.path.exists(explicit):
        print(f"[쿠키] YOUTUBE_COOKIES_FILE 사용: {explicit}")
        return explicit

    if os.path.exists("cookies.txt"):
        print("[쿠키] cookies.txt 사용")
        return "cookies.txt"

    print("[쿠키] 쿠키 없음 — GitHub Actions에서는 봇 감지 차단 가능")
    return None


def cleanup_cookies() -> None:
    global _COOKIE_TMPFILE
    if _COOKIE_TMPFILE and os.path.exists(_COOKIE_TMPFILE):
        try:
            os.unlink(_COOKIE_TMPFILE)
            print(f"[쿠키] 임시 파일 삭제: {_COOKIE_TMPFILE}")
        except Exception:
            pass
        _COOKIE_TMPFILE = None


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


def download_video(video_id: str, output_path: str, cookiefile: str | None) -> bool:
    opts = {
        "format": "worstvideo[ext=mp4]/worst[ext=mp4]/worstvideo/worst",
        "outtmpl": output_path,
        "quiet": False,
        "no_warnings": True,
        "retries": 5,
        "extractor_retries": 3,
        "fragment_retries": 5,
        "sleep_interval": 2,
        "max_sleep_interval": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        },
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        return True
    except Exception as e:
        err = str(e)
        if "Sign in" in err or "bot" in err.lower():
            print(
                f"  [봇 감지] {video_id}: 쿠키가 없거나 만료됨. "
                "YOUTUBE_COOKIES_B64 시크릿을 확인하세요."
            )
        else:
            print(f"  [다운로드 실패] {video_id}: {type(e).__name__}: {err[:300]}")
        return False


def extract_text_from_video(video_path: str, reader) -> str:
    cap = cv2.VideoCapture(video_path)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"  영상 정보: {src_w}x{src_h} / {fps:.1f}fps / 총 {total_frames}프레임 ({total_frames/fps:.0f}초)")

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
    # 실제 추출된 텍스트 전체 출력 (AI에 전달되는 내용 확인)
    if extracted_texts:
        print("  ---- OCR 추출 텍스트 시작 ----")
        for i, t in enumerate(extracted_texts):
            print(f"  [{i+1}] {t}")
        print("  ---- OCR 추출 텍스트 끝 ----")
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
    cookiefile = setup_cookies()

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
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    video_path = os.path.join(tmpdir, "video.mp4")
                    if download_video(video["id"], video_path, cookiefile) and os.path.exists(video_path):
                        text = extract_text_from_video(video_path, reader)
                    else:
                        text = ""
                summary = summarize(video["title"], text, client)
                send_discord(video, summary)
                print(f"  전송 완료")
            except Exception as e:
                print(f"  [오류] {video['title']}: {type(e).__name__}: {e}")
            time.sleep(1)

    print("\n모든 전송 완료!")
    cleanup_cookies()


if __name__ == "__main__":
    main()
