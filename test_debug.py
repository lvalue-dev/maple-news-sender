"""
각 컴포넌트 단독 진단 — SSL 무시, 하드코딩 영상 ID 사용
영상 ID: 봄빛 풍경 단풍빛 추억 이벤트 공략 영상 (테스트 출력에서 가져옴)
"""
import io
import json
import math
import re
import warnings
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound

_URL_RE = re.compile(r'https?://\S+|www\.\S+')
_CJK_RE = re.compile(r'[぀-ヿ㐀-䶿一-鿿豈-﫿]')

# 테스트할 영상 ID (채널: 메이플스토리 맑음)
# test_2026.py 실행 시 flat 추출된 첫 번째 영상 제목으로 확인
# 여러 개 시도해서 동작하는 것 찾기
TEST_VIDEO_IDS = [
    # 아래 ID들은 test_2026.py 출력에서 추론 - flat 추출로 가져온 영상들
    # 실제 ID를 모르므로 yt-dlp로 flat 추출 시도 (SSL 무시)
]

SESSION = requests.Session()
SESSION.verify = False  # 로컬 SSL 프록시 우회용


def get_video_ids_flat() -> list[dict]:
    """SSL 무시하고 채널에서 영상 ID 수집."""
    import yt_dlp

    CHANNEL_URL = "https://www.youtube.com/channel/UC1dHu9GhbHH7RcHKyJdaOvA/videos"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 5,
        "nocheckcertificate": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(CHANNEL_URL, download=False)
        entries = info.get("entries") or []
        return [{"id": e["id"], "title": e.get("title", "")} for e in entries if e]
    except Exception as e:
        print(f"flat 추출 실패: {e}")
        return []


def test_scrape(video_id: str):
    print(f"\n=== 페이지 스크래핑 테스트 (ID: {video_id}) ===")
    try:
        resp = SESSION.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
            timeout=15,
        )
    except Exception as e:
        print(f"  ❌ 요청 실패: {type(e).__name__}: {e}")
        return None

    print(f"  HTTP {resp.status_code}, 응답 크기: {len(resp.text)}자")

    if resp.status_code != 200:
        print(f"  응답 앞부분: {resp.text[:200]!r}")
        return None

    match = re.search(r'ytInitialPlayerResponse\s*=\s*', resp.text)
    if not match:
        print("  ❌ ytInitialPlayerResponse 없음")
        # 페이지 구조 힌트
        if "consent" in resp.text.lower():
            print("  → 동의 페이지로 보임")
        if "sign in" in resp.text.lower():
            print("  → 로그인 요구 페이지로 보임")
        print(f"  응답 앞 500자: {resp.text[:500]!r}")
        return None

    try:
        data, _ = json.JSONDecoder().raw_decode(resp.text, match.end())
    except Exception as e:
        print(f"  ❌ JSON 파싱 실패: {e}")
        snippet = resp.text[match.end():match.end()+200]
        print(f"  match 이후 200자: {snippet!r}")
        return None

    print(f"  JSON 최상위 키: {list(data.keys())[:8]}")

    video_details = data.get("videoDetails", {})
    desc = video_details.get("shortDescription", "")
    print(f"  shortDescription: {len(desc)}자")
    if desc:
        print(f"  설명 앞 150자: {desc[:150]!r}")

    storyboards = data.get("storyboards", {})
    spec = (storyboards.get("playerStoryboardSpecRenderer", {}).get("spec", "")
            or storyboards.get("playerLiveStoryboardSpecRenderer", {}).get("spec", ""))
    print(f"  storyboard spec: {'있음 (' + str(len(spec)) + '자)' if spec else '없음'}")
    if spec:
        print(f"  spec 앞 120자: {spec[:120]!r}")

    status = data.get("playabilityStatus", {})
    st = status.get("status", "없음")
    print(f"  playabilityStatus: {st}")
    if st not in ("OK", None):
        print(f"  차단 이유: {status.get('reason', '')} | {status.get('errorScreen', {})}")

    return {"description": desc, "storyboard_spec": spec}


def test_transcript_api(video_id: str):
    print(f"\n=== Transcript API 테스트 (ID: {video_id}) ===")
    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)
        print("  list() 호출 성공")
    except Exception as e:
        print(f"  ❌ list() 실패: {type(e).__name__}: {e}")
        return

    try:
        all_ts = list(transcript_list)
        print(f"  총 자막 {len(all_ts)}개:")
        for t in all_ts:
            print(f"    language_code={t.language_code!r}, is_generated={t.is_generated}, name={t.language!r}")
    except Exception as e:
        print(f"  목록 순회 오류: {e}")
        return

    # 수동 자막
    try:
        tl2 = YouTubeTranscriptApi().list(video_id)
        manual = tl2.find_manually_created_transcript(["ko", "ko-KR", "en"])
        entries = manual.fetch()
        print(f"  ✅ 수동 자막 {len(entries)}개 엔트리 (언어: {manual.language_code})")
        for e in entries[:5]:
            print(f"    [{e['start']:.1f}s] {e['text']!r}")
    except NoTranscriptFound:
        print("  수동 자막 없음 (NoTranscriptFound)")
    except Exception as e:
        print(f"  수동 자막 실패: {type(e).__name__}: {e}")

    # 자동 생성 자막도 확인 (내용 확인용)
    try:
        tl3 = YouTubeTranscriptApi().list(video_id)
        auto = tl3.find_generated_transcript(["ko", "ko-KR"])
        entries = auto.fetch()
        print(f"  자동 자막(ASR) {len(entries)}개 엔트리 (언어: {auto.language_code})")
        non_dot = [e for e in entries if re.search(r'[가-힣a-zA-Z0-9]', e['text'])]
        print(f"  한글/영문 포함 엔트리: {len(non_dot)}개")
        for e in non_dot[:5]:
            print(f"    [{e['start']:.1f}s] {e['text']!r}")
    except NoTranscriptFound:
        print("  자동 자막(ASR) 없음")
    except Exception as e:
        print(f"  자동 자막 실패: {type(e).__name__}: {e}")


def test_thumbnail_ocr(video_id: str):
    print(f"\n=== 썸네일 OCR 테스트 (ID: {video_id}) ===")
    try:
        from PIL import Image, ImageEnhance
        import easyocr
    except ImportError as e:
        print(f"  ❌ 패키지 없음: {e}")
        return

    print("  EasyOCR 초기화 중...")
    reader = easyocr.Reader(["ko", "en"], verbose=False)

    for quality in ("maxresdefault", "sddefault", "hqdefault"):
        url = f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"
        try:
            resp = SESSION.get(url, timeout=10)
        except Exception as e:
            print(f"  {quality}: 요청 실패 ({type(e).__name__})")
            continue
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        except Exception as e:
            print(f"  {quality}: 이미지 열기 실패 ({e})")
            continue

        print(f"  {quality}: {img.width}×{img.height}")
        if img.width < 300:
            print(f"    → 너무 작음 (빈 이미지)")
            continue

        from PIL import ImageEnhance
        img_enh = ImageEnhance.Contrast(img).enhance(1.3)
        result = reader.readtext(img_enh, detail=0, paragraph=True,
                                  text_threshold=0.5, low_text=0.3)
        raw = " ".join(result).strip()
        cleaned = _CJK_RE.sub('', raw)
        cleaned = _URL_RE.sub('', cleaned).strip()
        print(f"    OCR raw: {raw!r}")
        print(f"    cleaned: {cleaned!r}")
        has_ko = bool(re.search(r'[가-힣]', cleaned))
        print(f"    한국어: {'✅ 있음' if has_ko else '없음'}")
        if has_ko:
            break


def test_storyboard_ocr(spec: str, video_id: str = ""):
    if not spec:
        print(f"\n=== 스토리보드 OCR 테스트: spec 없음 (건너뜀) ===")
        return

    print(f"\n=== 스토리보드 OCR 테스트 ===")
    from PIL import Image, ImageEnhance
    import easyocr

    parts = spec.split("|")
    base_url = parts[0]
    levels = []
    for level_idx, params_str in enumerate(parts[1:]):
        p = params_str.split("#")
        if len(p) < 6:
            continue
        try:
            w, h, count, interval_ms, cols, rows = int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5])
        except ValueError:
            continue
        sigh = p[6] if len(p) > 6 else ""
        sheet_url_base = base_url.replace("$L", str(level_idx)).replace("$N", sigh)
        sheets_needed = math.ceil(count / (cols * rows))
        levels.append({"level": level_idx, "width": w, "height": h, "count": count,
                        "interval_ms": interval_ms, "cols": cols, "rows": rows,
                        "sheet_url_base": sheet_url_base, "sheets_needed": sheets_needed})
    levels.sort(key=lambda x: x["width"], reverse=True)

    print(f"  레벨:")
    for l in levels:
        marker = " ← 선택" if l["width"] >= 240 else ""
        print(f"    L{l['level']}: {l['width']}×{l['height']}, {l['count']}프레임, {l['interval_ms']}ms{marker}")

    best = next((l for l in levels if l["width"] >= 240), None)
    if not best:
        print("  ⚠️  240px 이상 없음, 가장 큰 레벨 사용")
        best = levels[0]
    if not best:
        return

    print(f"\n  선택: {best['width']}×{best['height']}")

    print("  EasyOCR 초기화...")
    reader = easyocr.Reader(["ko", "en"], verbose=False)

    url = best["sheet_url_base"] + "M0.jpg"
    print(f"  시트0 URL: {url[:100]}")
    try:
        resp = SESSION.get(url, timeout=15)
        sheet = Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        print(f"  ❌ 시트 다운로드 실패: {e}")
        return

    print(f"  시트 크기: {sheet.width}×{sheet.height}")
    cols, rows = best["cols"], best["rows"]
    frame_w, frame_h = best["width"], best["height"]

    found = []
    for r in range(min(rows, 3)):
        for c in range(min(cols, 5)):
            frame = sheet.crop((c * frame_w, r * frame_h, (c+1)*frame_w, (r+1)*frame_h))
            frame_up = frame.resize((frame_w * 4, frame_h * 4), Image.LANCZOS)
            frame_up = ImageEnhance.Contrast(frame_up).enhance(1.5)
            result = reader.readtext(frame_up, detail=0, paragraph=True,
                                      text_threshold=0.5, low_text=0.3)
            raw = " ".join(result).strip()
            cleaned = _CJK_RE.sub('', raw)
            cleaned = _URL_RE.sub('', cleaned).strip()
            if cleaned:
                has_ko = bool(re.search(r'[가-힣]', cleaned))
                print(f"    프레임[{r},{c}]: {cleaned!r} {'✅' if has_ko else '(한국어없음)'}")
                if has_ko:
                    found.append(cleaned)

    print(f"  결과: {len(found)}개 한국어 블록 발견")


if __name__ == "__main__":
    print("영상 ID 수집 중...")
    videos = get_video_ids_flat()
    if not videos:
        print("영상 목록 가져오기 실패, 수동 ID 사용")
        # 이전 test_2026.py 오류 메시지에서 본 ID
        videos = [{"id": "IR3ZweD2FYw", "title": "수동 지정"}]

    video_id = videos[0]["id"]
    title = videos[0]["title"]
    print(f"테스트 영상: {video_id} — {title}")

    scrape_result = test_scrape(video_id)
    test_transcript_api(video_id)
    test_thumbnail_ocr(video_id)

    spec = scrape_result.get("storyboard_spec", "") if scrape_result else ""
    test_storyboard_ocr(spec, video_id)
