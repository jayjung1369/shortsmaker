# Video Researcher
React + FastAPI 기반 영상 리서치/미디어 처리 웹앱 MVP.

## 기능
- YouTube 키워드 검색
- 조회수 / 업로드 날짜 필터
- 썸네일 카드형 UI
- 클릭해서 YouTube 영상 미리보기
- 영상 다운로드(허용된 콘텐츠에 한함)
- 다운로드 이력 JSON
- FFmpeg 음성 추출 API
- Instagram / Xiaohongshu 확장용 구조

> 플랫폼 약관과 저작권을 준수하세요. 타인 콘텐츠의 워터마크 제거/다운로드 제한 우회 기능은 포함하지 않습니다.

## Windows / VS Code 실행
### Backend
```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
FFmpeg가 PATH에 있어야 합니다.

### Frontend
```powershell
cd frontend
npm install
npm run dev
```
브라우저: http://localhost:5173


### Search accuracy update (v3)
The search API no longer relies on only the first 24 flat search entries. It collects a larger candidate pool (up to 200), then fetches full metadata for the candidates before applying view/date filters. This is important because `extract_flat=True` search results can omit fields such as upload date, and filtering those incomplete records can make known videos disappear.

You can also paste a direct YouTube video URL into the search box to inspect that exact video.


## v4 changes
- Keyword search only. No channel-search feature.
- Search results are not silently discarded because view count/date/thumbnail metadata is missing.
- User filters are applied only when the user explicitly enters them.
- Added **더 불러오기** pagination.
- Added real YouTube subtitle/caption extraction (`ko`, `en`, including automatic captions when available).
- Added video download and MP3 extraction buttons to cards and preview modal.
- `VITE_API_BASE` can be used to point the frontend at a deployed FastAPI backend.
- Downloads/captions are intended for content you own or are authorized to download/use, subject to YouTube/platform terms and copyright law.
