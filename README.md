# maple-news-sender

메이플스토리 공식 YouTube 채널의 새 영상을 감지하고, Gemini AI로 요약한 뒤 Discord로 전송하는 봇입니다.

## 동작 방식

1. GitHub Actions가 30분마다 YouTube RSS 피드를 폴링
2. `seen_videos.json`에 없는 새 영상 감지
3. Gemini 2.0 Flash API로 한국어 요약 생성
4. Discord Webhook으로 임베드 메시지 전송
5. 처리한 영상 ID를 `seen_videos.json`에 저장 후 자동 커밋

## 설정 방법

### 1. GitHub Secrets 등록

레포지토리 → Settings → Secrets and variables → Actions

| Secret 이름 | 값 |
|---|---|
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/app/apikey)에서 발급 |
| `DISCORD_WEBHOOK_URL` | Discord 채널 설정 → 연동 → 웹후크에서 복사 |

### 2. Actions 권한 설정

레포지토리 → Settings → Actions → General → Workflow permissions
→ **Read and write permissions** 선택 후 저장

### 3. 수동 실행 (선택)

Actions 탭 → YouTube Discord Notifier → Run workflow
