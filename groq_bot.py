import atexit
import base64
import json
import os
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

# ── 환경변수 ──────────────────────────────────────────────
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
_COOKIE_TMPFILE: str | None = None

_YDL_BASE_OPTS = {
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


# ── 쿠키 관리 ─────────────────────────────────────────────

def setup_cookies() -> str | None:
    """
    우선순위:
      1. YOUTUBE_COOKIES_B64 (base64) → 임시 파일 복원
      2. YOUTUBE_COOKIES_FILE (절대/상대 경로)
      3. 작업 디렉터리의 cookies.txt
    반환값: 쿠키 파일 경로 or None
    """
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
            _log_cookie_info(path)
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


def _log_cookie_info(path: str) -> None:
    try:
        with open(path) as f:
            lines = [l for l in f if not l.startswith("#") and "\t" in l]
        domains = sorted({l.split("\t")[0] for l in lines})
        names = sorted({l.split("\t")[5] for l in lines if len(l.split("\t")) > 5})
        print(f"  [쿠키 확인] {len(lines)}개 / 도메인: {', '.join(domains)}")
        print(f"  [쿠키 이름] {', '.join(names[:15])}{'...' if len(names) > 15 else ''}")
    except Exception as e:
        print(f"  [쿠키 확인 실패] {e}")


def cleanup_cookies() -> None:
    global _COOKIE_TMPFILE
    if _COOKIE_TMPFILE and os.path.exists(_COOKIE_TMPFILE):
        try:
            os.unlink(_COOKIE_TMPFILE)
            print(f"[쿠키] 임시 파일 삭제: {_COOKIE_TMPFILE}")
        except Exception:
            pass
        _COOKIE_TMPFILE = None


# ── 피드 ──────────────────────────────────────────────────

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
        videos.append({
            "id": video_id,
            "title": entry.get("title", ""),
            "link": entry.get("link", f"https://www.youtube.com/watch?v={video_id}"),
            "published": entry.get("published", datetime.now().isoformat()),
        })
    print(f"[RSS] {len(videos)}개 영상 수집")
    return videos[:15]


# ── 영상 다운로드 + OCR ────────────────────────────────────

def download_video(video_id: str, output_path: str, cookiefile: str | None) -> bool:
    opts = {
        **_YDL_BASE_OPTS,
        "format": "worstvideo[ext=mp4]/worst[ext=mp4]/worstvideo/worst",
        "extractor_args": {"youtube": {"player_client": ["web"]}},
        "outtmpl": output_path,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile

    client = opts.get("extractor_args", {}).get("youtube", {}).get("player_client", ["?"])
    print(f"  [yt-dlp] client={client} cookiefile={'있음' if cookiefile else '없음'}")

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
        elif "HTTP Error 429" in err or "rate" in err.lower():
            print(f"  [레이트 리밋] {video_id}: YouTube 요청 한도 초과")
        else:
            print(f"  [다운로드 실패] {video_id}: {type(e).__name__}: {err[:300]}")
        return False


def extract_text_from_video(video_path: str, reader) -> str:
    cap = cv2.VideoCapture(video_path)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"  [영상] {src_w}x{src_h} / {fps:.1f}fps / {total}프레임 ({total/fps:.0f}초)")

    sample_every = max(1, int(fps * FRAME_INTERVAL_SECONDS))
    extracted, last_text, count = [], "", 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        if count % sample_every == 0:
            resized = cv2.resize(frame, (640, 360))
            result = reader.readtext(resized, detail=0)
            text = _URL_RE.sub("", " ".join(result)).strip()
            if text and text != last_text and re.search(r"[가-힣]", text):
                extracted.append(text)
                last_text = text
        count += 1
    cap.release()

    print(f"  [OCR] {len(extracted)}개 블록 추출")
    if extracted:
        print("  ---- OCR 텍스트 시작 ----")
        for i, t in enumerate(extracted):
            print(f"  [{i+1}] {t}")
        print("  ---- OCR 텍스트 끝 ----")
    return "\n".join(extracted)


# ── Groq 요약 ──────────────────────────────────────────────

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
            "영상에서 텍스트를 추출하지 못했다. 제목에 명시된 내용만 불렛으로 정리해. "
            "추측이나 창작 금지. 한글만 사용. URL 절대 금지."
        )

    models = [GROQ_MODEL] + [m for m in GROQ_FALLBACK_MODELS if m != GROQ_MODEL]
    last_err = None
    for model in models:
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                )
                return resp.choices[0].message.content or ""
            except BadRequestError as e:
                if "model_decommissioned" in str(e):
                    last_err = e
                    break
                raise
            except Exception as e:
                if "429" in str(e) or "rate_limit" in str(e).lower():
                    wait = 30 * (attempt + 1)
                    print(f"  [Groq] Rate limit — {wait}초 대기")
                    time.sleep(wait)
                    continue
                raise
    raise last_err or RuntimeError("Groq: 사용 가능한 모델 없음")


# ── Discord 전송 ───────────────────────────────────────────

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


# ── 메인 ──────────────────────────────────────────────────

def main() -> None:
    cookiefile = setup_cookies()

    seen = load_seen()
    videos = fetch_feed()
    new_videos = [v for v in videos if v["id"] not in seen]

    if not new_videos:
        print("새 영상 없음")
        return

    print(f"새 영상 {len(new_videos)}개 처리 시작")
    reader = easyocr.Reader(["ko", "en"], verbose=False)
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    for video in reversed(new_videos):
        print(f"\n처리 중: {video['title']}")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                video_path = os.path.join(tmpdir, "video.mp4")
                if download_video(video["id"], video_path, cookiefile) and os.path.exists(video_path):
                    text = extract_text_from_video(video_path, reader)
                else:
                    text = ""
            summary = summarize(video["title"], text, client)
            send_discord(video, summary)
            seen.add(video["id"])
            print(f"  전송 완료: {video['title']}")
        except Exception as e:
            print(f"  [오류] {video['title']}: {type(e).__name__}: {e}")
            # 실패한 영상은 seen에 추가하지 않아 다음 실행 시 재시도

    save_seen(seen)
    cleanup_cookies()


if __name__ == "__main__":
    main()
