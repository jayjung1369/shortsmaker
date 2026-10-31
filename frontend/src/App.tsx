import { useState, useRef, useEffect } from "react";
import "./styles.css";

const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

function apiUrl(path: string) {
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

type Platform = "youtube" | "instagram" | "xiaohongshu" | "translator";
type SocialPlatform = "instagram" | "xiaohongshu";

type Item = {
  id: string;
  platform: string;
  title: string;
  url: string;
  thumbnail: string;
  channel: string;
  upload_date: string;
  duration: number;
  view_count: number | null;
  like_count: number | null;
  description: string;
  has_subtitles: boolean | null;
};

type BusyAction = "video" | "sub" | "audio";

type AccountStatus = {
  platform: SocialPlatform;
  connected: boolean;
  profile_dir: string;
  login_url: string;
};

type TranslateResult = {
  job_id: string;
  status?: "queued" | "running" | "completed" | "failed";
  stage?: string;
  message?: string;
  mode: "transcribe" | "translate";
  source_lang: string;
  target_lang: string;
  target_lang_label: string;
  quality: "fast" | "balanced" | "high";
  whisper_model: string;
  video_url: string;
  source_srt_url: string;
  translated_srt_url: string;
  transcript_url: string;
};

type SubtitlePosition = "top" | "middle" | "bottom";

type SubtitleSegment = {
  id: string;
  startSec: number;
  endSec: number;
  text: string;
};

const TABS: { id: Platform; label: string; icon: string }[] = [
  { id: "youtube", label: "YouTube", icon: "▶" },
  { id: "instagram", label: "Instagram", icon: "◆" },
  { id: "xiaohongshu", label: "Xiaohongshu", icon: "红" },
  { id: "translator", label: "Translator", icon: "文" },
];

const PLATFORM_PLACEHOLDER: Record<Platform, string> = {
  youtube: "검색어 또는 YouTube URL...",
  instagram: "Instagram 검색어 또는 URL...",
  xiaohongshu: "Xiaohongshu 검색어 또는 URL...",
  translator: "번역 탭에서는 파일을 업로드하세요.",
};

const PLATFORM_HINT: Record<Platform, string> = {
  youtube: "키워드 검색 · 직접 URL 입력 지원",
  instagram: "키워드 검색 우선, URL 입력도 지원",
  xiaohongshu: "키워드 검색 우선, URL 입력도 지원",
  translator: "업로드 → 음성 인식 → 번역 → 자막 합성",
};

const TRANSLATE_LANGUAGE_OPTIONS = [
  { value: "ko", label: "한국어" },
  { value: "en", label: "영어" },
  { value: "ja", label: "일본어" },
  { value: "zh-cn", label: "중국어 간체" },
  { value: "zh-tw", label: "중국어 번체" },
  { value: "es", label: "스페인어" },
  { value: "fr", label: "프랑스어" },
  { value: "de", label: "독일어" },
];

const TRANSLATE_MODE_OPTIONS = [
  { value: "transcribe", label: "원문 자막만" },
  { value: "translate", label: "번역 자막" },
];

const TRANSLATE_QUALITY_OPTIONS = [
  { value: "fast", label: "빠름 (tiny)" },
  { value: "balanced", label: "균형 (base)" },
  { value: "high", label: "고품질 (small)" },
];

const SUBTITLE_FONT_OPTIONS = [
  "Malgun Gothic",
  "NanumGothic",
  "Apple SD Gothic Neo",
  "Noto Sans CJK KR",
  "Arial",
  "Verdana",
];

function views(v: number | null) {
  if (v === null || v === undefined) return "조회수 정보 없음";
  if (v >= 100000000) return `${(v / 100000000).toFixed(1)}억`;
  if (v >= 10000) return `${(v / 10000).toFixed(1)}만`;
  if (v >= 1000) return `${(v / 1000).toFixed(1)}천`;
  return String(v);
}

function date(v: string) {
  if (!v) return "-";
  return `${v.slice(0, 4)}-${v.slice(4, 6)}-${v.slice(6, 8)}`;
}

function duration(v: number) {
  if (!v) return "";
  const h = Math.floor(v / 3600);
  const m = Math.floor((v % 3600) / 60);
  const s = v % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function formatViews(v: string) {
  const d = v.replace(/[^0-9]/g, "");
  return d ? Number(d).toLocaleString("ko-KR") : "";
}

function parseViews(v: string) {
  return v.replace(/[^0-9]/g, "");
}

function isYoutubeItem(item: Item) {
  return item.platform === "youtube";
}

function srtTimeToSec(v: string) {
  const m = v.trim().match(/^(\d{2}):(\d{2}):(\d{2})[,\.](\d{1,3})$/);
  if (!m) return 0;
  const hh = Number(m[1]);
  const mm = Number(m[2]);
  const ss = Number(m[3]);
  const ms = Number(m[4].padEnd(3, "0"));
  return hh * 3600 + mm * 60 + ss + ms / 1000;
}

function secToSrtTime(sec: number) {
  const totalMs = Math.max(0, Math.round((Number.isFinite(sec) ? sec : 0) * 1000));
  const hh = Math.floor(totalMs / 3600000);
  const mm = Math.floor((totalMs % 3600000) / 60000);
  const ss = Math.floor((totalMs % 60000) / 1000);
  const ms = totalMs % 1000;
  return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")},${String(ms).padStart(3, "0")}`;
}

function parseSrtToSegments(srt: string): SubtitleSegment[] {
  const blocks = srt.replace(/\r/g, "").trim().split(/\n\s*\n/);
  const out: SubtitleSegment[] = [];

  for (let i = 0; i < blocks.length; i += 1) {
    const lines = blocks[i].split("\n").map((x) => x.trimEnd());
    if (!lines.length) continue;
    const arrowIdx = lines.findIndex((l) => l.includes("-->"));
    if (arrowIdx < 0) continue;

    const [startRaw, endRaw] = lines[arrowIdx].split("-->").map((x) => x.trim());
    const startSec = srtTimeToSec(startRaw);
    const endSec = srtTimeToSec(endRaw);
    const text = lines.slice(arrowIdx + 1).join("\n").trim();
    if (!text) continue;
    out.push({
      id: `seg-${i}-${startSec.toFixed(3)}`,
      startSec,
      endSec: endSec > startSec ? endSec : startSec + 0.8,
      text,
    });
  }

  return out;
}

function buildSrtFromSegments(segments: SubtitleSegment[]) {
  return segments
    .slice()
    .sort((a, b) => a.startSec - b.startSec)
    .map((seg, idx) => {
      const start = secToSrtTime(seg.startSec);
      const end = secToSrtTime(Math.max(seg.endSec, seg.startSec + 0.1));
      return `${idx + 1}\n${start} --> ${end}\n${seg.text.trim()}\n`;
    })
    .join("\n");
}

export default function App() {
  const [platform, setPlatform] = useState<Platform>("youtube");

  const [query, setQuery] = useState("");
  const [minViews, setMinViews] = useState("");
  const [maxViews, setMaxViews] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [results, setResults] = useState<Item[]>([]);
  const [hasMore, setHasMore] = useState(false);

  const [instagramQuery, setInstagramQuery] = useState("");
  const [xiaohongshuQuery, setXiaohongshuQuery] = useState("");
  const [socialItems, setSocialItems] = useState<Item[]>([]);
  const [socialHasMore, setSocialHasMore] = useState(false);
  const [accountStatus, setAccountStatus] = useState<AccountStatus | null>(null);
  const [accountBusy, setAccountBusy] = useState(false);
  const [accountPanelOpen, setAccountPanelOpen] = useState(false);
  const [translateFile, setTranslateFile] = useState<File | null>(null);
  const [translateMode, setTranslateMode] = useState<"transcribe" | "translate">("transcribe");
  const [targetLang, setTargetLang] = useState("en");
  const [translateQuality, setTranslateQuality] = useState<"fast" | "balanced" | "high">("high");
  const [translateBusy, setTranslateBusy] = useState(false);
  const [translateResult, setTranslateResult] = useState<TranslateResult | null>(null);
  const [subtitleDraft, setSubtitleDraft] = useState("");
  const [subtitleSourceKind, setSubtitleSourceKind] = useState<"source" | "translated">("source");
  const [subtitleFontFamily, setSubtitleFontFamily] = useState("Malgun Gothic");
  const [subtitleFontSize, setSubtitleFontSize] = useState(28);
  const [subtitlePosition, setSubtitlePosition] = useState<SubtitlePosition>("bottom");
  const [subtitleMarginV, setSubtitleMarginV] = useState(28);
  const [subtitleRenderBusy, setSubtitleRenderBusy] = useState(false);
  const [subtitleSegments, setSubtitleSegments] = useState<SubtitleSegment[]>([]);

  const [selected, setSelected] = useState<Item | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [previewReady, setPreviewReady] = useState(false);
  const searchAbortRef = useRef<AbortController | null>(null);
  const actionAbortRef = useRef<AbortController | null>(null);

  const visibleResults = platform === "youtube" ? results : platform === "translator" ? [] : socialItems;

  useEffect(() => {
    document.documentElement.dataset.platform = platform;
  }, [platform]);

  useEffect(() => {
    if (platform === "youtube" || platform === "translator") {
      setAccountStatus(null);
      return;
    }
    void refreshAccountStatus(platform);
  }, [platform]);

  useEffect(() => {
    if (!translateResult) {
      setSubtitleDraft("");
      return;
    }

    const preferred =
      translateResult.mode === "translate" && translateResult.translated_srt_url
        ? { url: translateResult.translated_srt_url, kind: "translated" as const }
        : { url: translateResult.source_srt_url, kind: "source" as const };

    void loadSubtitleDraft(preferred.url, preferred.kind);
  }, [translateResult]);

  function switchPlatform(next: Platform) {
    setPlatform(next);
    setMessage("");
    setSelected(null);
    setPreviewReady(false);
    setAccountPanelOpen(false);
  }

  function activeQuery() {
    if (platform === "youtube") return query;
    if (platform === "translator") return "";
    if (platform === "instagram") return instagramQuery;
    return xiaohongshuQuery;
  }

  function setActiveQuery(v: string) {
    if (platform === "youtube") {
      setQuery(v);
      return;
    }
    if (platform === "translator") return;
    if (platform === "instagram") {
      setInstagramQuery(v);
      return;
    }
    setXiaohongshuQuery(v);
  }

  async function refreshAccountStatus(nextPlatform: SocialPlatform) {
    try {
      const r = await fetch(apiUrl(`/api/account/status?platform=${nextPlatform}`));
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "계정 상태 조회 실패");
      setAccountStatus(data);
    } catch {
      setAccountStatus(null);
    }
  }

  async function connectAccount() {
    if (platform === "youtube" || platform === "translator") return;
    setAccountBusy(true);
    try {
      const r = await fetch(apiUrl(`/api/account/connect?platform=${platform}`), { method: "POST" });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "계정 연결 실패");
      setMessage(data.message || "앱 전용 로그인 창을 열었습니다. 그 창에서 로그인한 뒤 닫으면 세션이 저장됩니다.");
      setAccountPanelOpen(true);
      await refreshAccountStatus(platform);
    } catch (e: any) {
      setMessage(e.message);
    } finally {
      setAccountBusy(false);
    }
  }

  async function disconnectAccount() {
    if (platform === "youtube" || platform === "translator") return;
    setAccountBusy(true);
    try {
      const r = await fetch(apiUrl(`/api/account/disconnect?platform=${platform}`), { method: "POST" });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "계정 해제 실패");
      setMessage(data.message || "연결을 해제했습니다.");
      setAccountPanelOpen(false);
      await refreshAccountStatus(platform);
    } catch (e: any) {
      setMessage(e.message);
    } finally {
      setAccountBusy(false);
    }
  }

  function isBusy(action: BusyAction, id: string) {
    return busy === `${action}:${id}`;
  }

  function currentBusyText(item: Item) {
    if (isBusy("video", item.id)) return "영상 다운로드 진행 중";
    if (isBusy("sub", item.id)) return "자막 추출 진행 중";
    if (isBusy("audio", item.id)) return "MP3 추출 진행 중";
    return "";
  }

  function openPreview(item: Item) {
    setPreviewReady(false);
    setSelected(item);
  }

  function closePreview() {
    setPreviewReady(false);
    setSelected(null);
  }

  function getYoutubeEmbedSrc(videoId: string) {
    const origin = window.location.origin;
    const params = new URLSearchParams({
      autoplay: "0",
      controls: "1",
      fs: "1",
      playsinline: "1",
      rel: "0",
      modestbranding: "1",
      iv_load_policy: "3",
      enablejsapi: "1",
      origin,
    });
    return `https://www.youtube-nocookie.com/embed/${videoId}?${params.toString()}`;
  }

  function cancelSearch() {
    searchAbortRef.current?.abort();
    searchAbortRef.current = null;
    setLoading(false);
    setMessage("조회가 취소됐습니다.");
  }

  async function searchYoutube(reset = true) {
    const q = query.trim();
    if (!q) {
      setMessage("검색어 또는 URL을 입력하세요.");
      return;
    }

    searchAbortRef.current?.abort();
    const ctrl = new AbortController();
    searchAbortRef.current = ctrl;

    const nextOffset = reset ? 0 : results.length;
    setLoading(true);
    setMessage("");
    try {
      const p = new URLSearchParams({ q, limit: "24", offset: String(nextOffset) });
      if (parseViews(minViews)) p.set("min_views", parseViews(minViews));
      if (parseViews(maxViews)) p.set("max_views", parseViews(maxViews));
      if (from) p.set("date_from", from);
      if (to) p.set("date_to", to);

      const r = await fetch(apiUrl(`/api/search?${p}`), { signal: ctrl.signal });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "검색 실패");

      setResults((prev) => (reset ? data.items : [...prev, ...data.items]));
      setHasMore(Boolean(data.has_more));
      if (!data.items.length && reset) setMessage("검색 결과가 없습니다.");
    } catch (e: any) {
      if (e.name !== "AbortError") setMessage(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function searchSocial(reset = true) {
    const q = activeQuery().trim();
    if (!q) {
      setMessage("검색어를 입력하세요.");
      return;
    }

    searchAbortRef.current?.abort();
    const ctrl = new AbortController();
    searchAbortRef.current = ctrl;

    const nextOffset = reset ? 0 : socialItems.length;
    setLoading(true);
    setMessage("");
    try {
      if (q.startsWith("http://") || q.startsWith("https://")) {
        const infoP = new URLSearchParams({ url: q });
        const infoReq = await fetch(apiUrl(`/api/info?${infoP.toString()}`), { signal: ctrl.signal });
        const infoData = await infoReq.json();
        if (!infoReq.ok) throw new Error(infoData.detail || "조회 실패");
        setSocialItems([infoData]);
        setSocialHasMore(false);
        return;
      }

      const p = new URLSearchParams({
        platform,
        q,
        limit: "24",
        offset: String(nextOffset),
      });
      if (parseViews(minViews)) p.set("min_views", parseViews(minViews));
      if (parseViews(maxViews)) p.set("max_views", parseViews(maxViews));
      if (from) p.set("date_from", from);
      if (to) p.set("date_to", to);
      const r = await fetch(apiUrl(`/api/search-social?${p.toString()}`), { signal: ctrl.signal });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "조회 실패");

      setSocialItems((prev) => (reset ? data.items : [...prev, ...data.items]));
      setSocialHasMore(Boolean(data.has_more));
      if (!data.items.length && reset) {
        setMessage("검색 결과가 없습니다. 소셜 플랫폼은 차단이 잦아 로컬 브라우저 수집 모드(로그인 포함)가 필요할 수 있습니다. URL 직접 입력도 가능합니다.");
      }
    } catch (e: any) {
      if (e.name !== "AbortError") setMessage(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function search(reset = true) {
    if (platform === "translator") return;
    if (platform === "youtube") {
      await searchYoutube(reset);
      return;
    }
    await searchSocial(reset);
  }

  function triggerDownloadFromApiPath(path: string, fallbackName: string) {
    const fileName = decodeURIComponent(path.split("/").pop() || fallbackName);
    triggerDownload(apiUrl(path), fileName);
  }

  async function loadSubtitleDraft(path: string, kind: "source" | "translated") {
    try {
      const req = await fetch(apiUrl(path));
      const text = await req.text();
      if (!req.ok) throw new Error("자막 텍스트를 불러오지 못했습니다.");
      setSubtitleDraft(text);
      setSubtitleSegments(parseSrtToSegments(text));
      setSubtitleSourceKind(kind);
    } catch (e: any) {
      setMessage(e.message || "자막 텍스트를 불러오지 못했습니다.");
    }
  }

  async function waitForTranslateResult(statusUrl: string, ctrl: AbortController) {
    while (true) {
      if (ctrl.signal.aborted) throw new DOMException("Aborted", "AbortError");
      await new Promise((resolve) => setTimeout(resolve, 2000));

      const req = await fetch(apiUrl(statusUrl), { signal: ctrl.signal });
      const raw = await req.text();
      let data: any = {};
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        data = { detail: raw || "번역 작업 상태 조회 실패" };
      }
      if (!req.ok) throw new Error(data.detail || data.message || "번역 작업 상태 조회 실패");

      if (data.status === "failed") {
        throw new Error(data.detail || data.message || "번역 작업이 실패했습니다.");
      }
      if (data.stage || data.message) {
        setMessage(data.message || "번역 작업을 처리 중입니다...");
      }
      if (data.status === "completed") {
        return data;
      }
    }
  }

  function syncSegmentsFromDraft() {
    const parsed = parseSrtToSegments(subtitleDraft);
    if (!parsed.length) {
      setMessage("SRT 파싱 실패: 시간 구간(-->) 형식을 확인하세요.");
      return;
    }
    setSubtitleSegments(parsed);
    setMessage(`타임라인 편집으로 ${parsed.length}개 자막을 불러왔습니다.`);
  }

  function syncDraftFromSegments(nextSegments: SubtitleSegment[]) {
    const srt = buildSrtFromSegments(nextSegments);
    setSubtitleDraft(srt);
  }

  function updateSegment(id: string, field: "startSec" | "endSec" | "text", value: string) {
    setSubtitleSegments((prev) => {
      const next = prev.map((seg) => {
        if (seg.id !== id) return seg;
        if (field === "text") return { ...seg, text: value };
        const num = Number(value);
        if (!Number.isFinite(num)) return seg;
        return { ...seg, [field]: Math.max(0, num) };
      });
      syncDraftFromSegments(next);
      return next;
    });
  }

  function removeSegment(id: string) {
    setSubtitleSegments((prev) => {
      const next = prev.filter((seg) => seg.id !== id);
      syncDraftFromSegments(next);
      return next;
    });
  }

  function addSegment() {
    setSubtitleSegments((prev) => {
      const last = prev[prev.length - 1];
      const start = last ? Math.max(last.endSec, last.startSec + 0.2) : 0;
      const next = [
        ...prev,
        {
          id: `seg-new-${Date.now()}`,
          startSec: start,
          endSec: start + 1.5,
          text: "새 자막",
        },
      ];
      syncDraftFromSegments(next);
      return next;
    });
  }

  async function translateVideo() {
    if (!translateFile) {
      setMessage("번역할 동영상 파일을 먼저 선택하세요.");
      return;
    }

    actionAbortRef.current?.abort();
    const ctrl = new AbortController();
    actionAbortRef.current = ctrl;
    setTranslateBusy(true);
    setMessage(
      translateMode === "translate"
        ? "음성 인식/번역/자막 합성 중입니다. 잠시만 기다려주세요..."
        : "음성 인식/자막 합성 중입니다. 잠시만 기다려주세요..."
    );

    try {
      const form = new FormData();
      form.append("video", translateFile);
      form.append("mode", translateMode);
      form.append("target_lang", targetLang);
      form.append("quality", translateQuality);

      const req = await fetch(apiUrl("/api/translate-video"), {
        method: "POST",
        body: form,
        signal: ctrl.signal,
      });
      const raw = await req.text();
      let data: any = {};
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        data = { detail: raw || "번역 처리 실패" };
      }
      if (!req.ok) throw new Error(data.detail || data.message || raw || "번역 처리 실패");

      const finalData = data.status === "completed" ? data : await waitForTranslateResult(data.status_url, ctrl);

      setTranslateResult(finalData);
      setSubtitleDraft("");
      setMessage(
        finalData.mode === "translate"
          ? "번역이 완료되었습니다. 번역 영상과 자막 파일을 다운로드하세요."
          : "자막 생성이 완료되었습니다. 자막 합성 영상과 원문 자막을 다운로드하세요."
      );
    } catch (e: any) {
      if (e.name !== "AbortError") setMessage(e.message || "번역 실패");
    } finally {
      setTranslateBusy(false);
    }
  }

  async function renderEditedSubtitles() {
    if (!translateResult) return;
    if (!subtitleDraft.trim()) {
      setMessage("자막 내용이 비어 있습니다.");
      return;
    }

    setSubtitleRenderBusy(true);
    setMessage("수정한 자막으로 영상을 다시 렌더링 중입니다...");
    try {
      const req = await fetch(apiUrl("/api/translate-video/render-subtitles"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: translateResult.job_id,
          srt_text: subtitleDraft,
          font_family: subtitleFontFamily,
          font_size: subtitleFontSize,
          position: subtitlePosition,
          margin_v: subtitleMarginV,
          quality: translateQuality,
        }),
      });
      const data = await req.json();
      if (!req.ok) throw new Error(data.detail || "자막 재합성 실패");

      setTranslateResult((prev) => (prev ? { ...prev, video_url: data.video_url } : prev));
      setMessage("수정 자막 반영 영상이 생성되었습니다.");
    } catch (e: any) {
      setMessage(e.message || "자막 재합성 실패");
    } finally {
      setSubtitleRenderBusy(false);
    }
  }

  function triggerDownload(url: string, filename: string) {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  function filenameFromDisposition(disposition: string | null, fallback: string) {
    if (!disposition) return fallback;
    const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf8Match?.[1]) {
      try {
        return decodeURIComponent(utf8Match[1]);
      } catch {
        return utf8Match[1];
      }
    }
    const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
    return plainMatch?.[1] || fallback;
  }

  async function downloadBinaryResponse(req: Response, fallbackName: string) {
    const disposition = req.headers.get("content-disposition");
    const filename = filenameFromDisposition(disposition, fallbackName);
    const blob = await req.blob();
    const objectUrl = URL.createObjectURL(blob);
    triggerDownload(objectUrl, filename);
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000);
  }

  function cancelAction() {
    actionAbortRef.current?.abort();
    actionAbortRef.current = null;
    setBusy("");
    setMessage("작업이 취소됐습니다.");
  }

  async function download(item: Item) {
    actionAbortRef.current?.abort();
    const ctrl = new AbortController();
    actionAbortRef.current = ctrl;
    setBusy(`video:${item.id}`);
    setMessage("영상 다운로드 중...");

    try {
      const endpoint = isYoutubeItem(item) ? apiUrl(`/api/download/${item.id}`) : apiUrl("/api/download-url");
      const req = await fetch(endpoint, {
        method: "POST",
        headers: isYoutubeItem(item) ? undefined : { "Content-Type": "application/json" },
        body: isYoutubeItem(item) ? undefined : JSON.stringify({ url: item.url }),
        signal: ctrl.signal,
      });
      if (!req.ok) {
        const data = await req.json().catch(() => ({}));
        throw new Error(data.detail || "다운로드 실패");
      }

      await downloadBinaryResponse(req, `${item.id}.mp4`);
      setMessage("영상 저장 완료 - 다운로드 폴더를 확인하세요.");
    } catch (e: any) {
      if (e.name !== "AbortError") setMessage(e.message);
    } finally {
      setBusy("");
    }
  }

  async function subtitles(item: Item) {
    actionAbortRef.current?.abort();
    const ctrl = new AbortController();
    actionAbortRef.current = ctrl;
    setBusy(`sub:${item.id}`);
    setMessage("자막 추출 중...");

    try {
      const endpoint = isYoutubeItem(item) ? apiUrl(`/api/subtitles/${item.id}`) : apiUrl("/api/subtitles-url");
      const req = await fetch(endpoint, {
        method: "POST",
        headers: isYoutubeItem(item) ? undefined : { "Content-Type": "application/json" },
        body: isYoutubeItem(item) ? undefined : JSON.stringify({ url: item.url }),
        signal: ctrl.signal,
      });
      if (!req.ok) {
        const data = await req.json().catch(() => ({}));
        throw new Error(data.detail || "자막이 없습니다.");
      }

      await downloadBinaryResponse(req, `${item.id}_subtitles.zip`);
      setMessage("자막 저장 완료 - 다운로드 폴더를 확인하세요.");
    } catch (e: any) {
      if (e.name !== "AbortError") setMessage(e.message);
    } finally {
      setBusy("");
    }
  }

  async function audio(item: Item) {
    actionAbortRef.current?.abort();
    const ctrl = new AbortController();
    actionAbortRef.current = ctrl;
    setBusy(`audio:${item.id}`);
    setMessage("MP3 추출 중...");

    try {
      const req = isYoutubeItem(item)
        ? await fetch(apiUrl(`/api/audio/${item.id}`), { method: "POST", signal: ctrl.signal })
        : await fetch(apiUrl("/api/audio-url"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: item.url }),
            signal: ctrl.signal,
          });
      if (!req.ok) {
        const data = await req.json().catch(() => ({}));
        throw new Error(data.detail || "오디오 추출 실패");
      }

      await downloadBinaryResponse(req, `${item.id}.mp3`);
      setMessage("MP3 저장 완료 - 다운로드 폴더를 확인하세요.");
    } catch (e: any) {
      if (e.name !== "AbortError") setMessage(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="app">
      {loading && (
        <div className="search-overlay" aria-live="polite" aria-label="조회 중">
          <div className="search-spinner" />
          <span>조회 중...</span>
          <button type="button" className="btn-cancel overlay-cancel" onClick={cancelSearch}>✕ 취소</button>
        </div>
      )}

      <header className="hero">
        <div className="eyebrow">VIDEO RESEARCHER</div>
        <h1>One workflow, three platforms.</h1>
        <p>탭으로 플랫폼을 전환하고, 같은 흐름으로 조회·저장·자막·MP3를 처리합니다.</p>

        <div className="platform-tabs" role="tablist" aria-label="플랫폼 선택">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={platform === tab.id}
              className={`tab-btn${platform === tab.id ? " is-active" : ""}`}
              onClick={() => switchPlatform(tab.id)}
            >
              <span>{tab.icon}</span>
              <strong>{tab.label}</strong>
            </button>
          ))}
        </div>
      </header>

      {platform === "translator" ? (
        <section className="translate-panel">
          <div className="translate-head">
            <h2>동영상 번역</h2>
            <p>동영상을 업로드하면 음성을 텍스트로 변환해 자막을 입힙니다. 필요하면 번역 자막 모드로 바꿀 수 있습니다.</p>
          </div>

          <div className="translate-grid">
            <label className="translate-file">
              <span>동영상 파일</span>
              <input
                type="file"
                accept="video/mp4,video/quicktime,video/x-matroska,video/webm,video/x-msvideo,video/x-m4v"
                onChange={(e) => {
                  setTranslateFile(e.target.files?.[0] || null);
                  setTranslateResult(null);
                }}
              />
              <strong>{translateFile?.name || "선택된 파일 없음"}</strong>
            </label>

            <label className="translate-select">
              <span>처리 모드</span>
              <select value={translateMode} onChange={(e) => setTranslateMode(e.target.value as "transcribe" | "translate")}> 
                {TRANSLATE_MODE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </label>

            <label className="translate-select">
              <span>품질</span>
              <select value={translateQuality} onChange={(e) => setTranslateQuality(e.target.value as "fast" | "balanced" | "high")}> 
                {TRANSLATE_QUALITY_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </label>

            <label className="translate-select" style={{ display: translateMode === "translate" ? "grid" : "none" }}>
              <span>번역 언어</span>
              <select value={targetLang} onChange={(e) => setTargetLang(e.target.value)}>
                {TRANSLATE_LANGUAGE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </label>

            <button type="button" onClick={translateVideo} disabled={translateBusy || !translateFile}>
              {translateBusy ? <><span className="btn-spinner" />처리 중...</> : "번역 시작"}
            </button>
          </div>

          {translateResult && (
            <div className="translate-result">
              <div className="translate-meta">
                <strong>작업 ID: {translateResult.job_id}</strong>
                <span>
                  모드: {translateResult.mode === "translate" ? "번역 자막" : "원문 자막"} · 품질: {translateResult.quality} ({translateResult.whisper_model})
                </span>
                <span>
                  감지 언어: {translateResult.source_lang}
                  {translateResult.mode === "translate" ? ` → 번역 언어: ${translateResult.target_lang_label}` : ""}
                </span>
              </div>
              <div className="translate-actions">
                <button type="button" onClick={() => triggerDownloadFromApiPath(translateResult.video_url, "subbed.mp4")}>자막 영상 다운로드</button>
                {translateResult.mode === "translate" && translateResult.translated_srt_url && (
                  <button type="button" onClick={() => triggerDownloadFromApiPath(translateResult.translated_srt_url, "translated.srt")}>번역 자막 다운로드</button>
                )}
                <button type="button" onClick={() => triggerDownloadFromApiPath(translateResult.source_srt_url, "source.srt")}>원문 자막 다운로드</button>
                <button type="button" onClick={() => triggerDownloadFromApiPath(translateResult.transcript_url, "transcript.txt")}>원문 텍스트 다운로드</button>
              </div>

              <div className="subtitle-editor">
                <div className="subtitle-editor-head">
                  <strong>자막 편집</strong>
                  <div className="subtitle-editor-source">
                    <button
                      type="button"
                      className={subtitleSourceKind === "source" ? "is-active" : ""}
                      onClick={() => loadSubtitleDraft(translateResult.source_srt_url, "source")}
                    >
                      원문 불러오기
                    </button>
                    {translateResult.mode === "translate" && translateResult.translated_srt_url && (
                      <button
                        type="button"
                        className={subtitleSourceKind === "translated" ? "is-active" : ""}
                        onClick={() => loadSubtitleDraft(translateResult.translated_srt_url, "translated")}
                      >
                        번역본 불러오기
                      </button>
                    )}
                  </div>
                </div>

                <textarea
                  className="subtitle-editor-text"
                  value={subtitleDraft}
                  onChange={(e) => setSubtitleDraft(e.target.value)}
                  placeholder="여기에 SRT 자막 내용을 수정하세요"
                />

                <div className="subtitle-timeline-tools">
                  <button type="button" onClick={syncSegmentsFromDraft}>SRT를 타임라인으로 불러오기</button>
                  <button type="button" onClick={addSegment}>자막 줄 추가</button>
                  <span>{subtitleSegments.length} lines</span>
                </div>

                {subtitleSegments.length > 0 && (
                  <div className="subtitle-timeline">
                    {subtitleSegments.map((seg, idx) => (
                      <div className="subtitle-row" key={seg.id}>
                        <span className="subtitle-row-index">{idx + 1}</span>
                        <input
                          type="number"
                          step="0.1"
                          min={0}
                          value={Number(seg.startSec.toFixed(3))}
                          onChange={(e) => updateSegment(seg.id, "startSec", e.target.value)}
                        />
                        <input
                          type="number"
                          step="0.1"
                          min={0}
                          value={Number(seg.endSec.toFixed(3))}
                          onChange={(e) => updateSegment(seg.id, "endSec", e.target.value)}
                        />
                        <input
                          type="text"
                          value={seg.text}
                          onChange={(e) => updateSegment(seg.id, "text", e.target.value)}
                        />
                        <button type="button" className="subtitle-row-delete" onClick={() => removeSegment(seg.id)}>
                          삭제
                        </button>
                      </div>
                    ))}
                  </div>
                )}

                <div className="subtitle-style-grid">
                  <label>
                    <span>폰트</span>
                    <select value={subtitleFontFamily} onChange={(e) => setSubtitleFontFamily(e.target.value)}>
                      {SUBTITLE_FONT_OPTIONS.map((font) => (
                        <option key={font} value={font}>{font}</option>
                      ))}
                    </select>
                  </label>

                  <label>
                    <span>폰트 크기</span>
                    <input
                      type="number"
                      min={12}
                      max={80}
                      value={subtitleFontSize}
                      onChange={(e) => setSubtitleFontSize(Number(e.target.value || 28))}
                    />
                  </label>

                  <label>
                    <span>위치</span>
                    <select value={subtitlePosition} onChange={(e) => setSubtitlePosition(e.target.value as SubtitlePosition)}>
                      <option value="top">상단</option>
                      <option value="middle">중앙</option>
                      <option value="bottom">하단</option>
                    </select>
                  </label>

                  <label>
                    <span>세로 여백</span>
                    <input
                      type="number"
                      min={0}
                      max={200}
                      value={subtitleMarginV}
                      onChange={(e) => setSubtitleMarginV(Number(e.target.value || 28))}
                    />
                  </label>
                </div>

                <div className="subtitle-editor-actions">
                  <button type="button" onClick={renderEditedSubtitles} disabled={subtitleRenderBusy || !subtitleDraft.trim()}>
                    {subtitleRenderBusy ? <><span className="btn-spinner dark" />자막 다시 입히는 중...</> : "수정 자막 반영"}
                  </button>
                </div>
              </div>

              <video controls src={apiUrl(translateResult.video_url)} className="translated-preview" />
            </div>
          )}
        </section>
      ) : (
      <section className="search-panel">
        <div className="search-main">
          <input
            value={activeQuery()}
            onChange={(e) => setActiveQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && search(true)}
            placeholder={PLATFORM_PLACEHOLDER[platform]}
          />
          <button type="button" className={loading ? "btn-cancel" : ""} onClick={loading ? cancelSearch : () => search(true)}>
            {loading ? <><span className="btn-spinner" />취소</> : "조회"}
          </button>
        </div>

        <div className="platform-hint">{PLATFORM_HINT[platform]}</div>

        {platform !== "youtube" && (
          <div className="account-panel">
            <div className="account-status">
              <span className={`account-badge${accountStatus?.connected ? " is-on" : ""}`}>
                {accountStatus?.connected ? "계정 연결됨" : "계정 미연결"}
              </span>
              <span className="account-path">{accountStatus?.profile_dir || "연결 프로필 없음"}</span>
            </div>
            <div className="account-actions">
              <button type="button" onClick={connectAccount} disabled={accountBusy}>
                {accountBusy ? "연결 중..." : "계정 연결"}
              </button>
              <button type="button" className="btn-cancel" onClick={disconnectAccount} disabled={accountBusy || !accountStatus?.connected}>
                연결 해제
              </button>
            </div>
          </div>
        )}

        {platform !== "youtube" && accountPanelOpen && (
          <div className="account-modal-backdrop" onClick={() => setAccountPanelOpen(false)}>
            <div className="account-modal" onClick={(e) => e.stopPropagation()}>
              <div className="eyebrow">ACCOUNT CONNECT</div>
              <h2>{platform === "instagram" ? "Instagram" : "Xiaohongshu"} 로그인 화면</h2>
              <p>계정 연결을 누르면 앱 전용 로그인 창이 열립니다. 그 창에서 로그인한 뒤 닫고, 이 화면으로 돌아와 다시 상태를 확인하세요.</p>
              <div className="account-modal-actions">
                <a href={accountStatus?.login_url || "#"} target="_blank" rel="noreferrer">로그인 페이지 열기</a>
                <button type="button" className="btn-cancel" onClick={() => setAccountPanelOpen(false)}>닫기</button>
              </div>
            </div>
          </div>
        )}

        <div className="filters">
          <input
            value={formatViews(minViews)}
            onChange={(e) => setMinViews(parseViews(e.target.value))}
            placeholder="최소 조회수 (선택)"
            inputMode="numeric"
          />
          <input
            value={formatViews(maxViews)}
            onChange={(e) => setMaxViews(parseViews(e.target.value))}
            placeholder="최대 조회수 (선택)"
            inputMode="numeric"
          />
          <input value={from} onChange={(e) => setFrom(e.target.value)} type="date" />
          <input value={to} onChange={(e) => setTo(e.target.value)} type="date" />
        </div>
      </section>
      )}

      {message && (
        <div className="notice" aria-live="polite">
          {message}
          <button type="button" className="notice-close" onClick={() => setMessage("")}>✕</button>
        </div>
      )}

      {platform !== "translator" && <div className="result-head">
        <strong>{visibleResults.length.toLocaleString()} results</strong>
        <span>{platform === "youtube" ? "필터를 입력하지 않으면 검색 결과를 임의로 제외하지 않습니다." : "키워드로 찾은 결과를 플랫폼 탭별로 표시합니다."}</span>
      </div>}

      {platform !== "translator" && <main className="grid">
        {visibleResults.map((item) => (
          <article className="card" key={`${item.platform}:${item.id}`}>
            <button type="button" className="thumb" onClick={() => openPreview(item)}>
              {item.thumbnail ? <img src={item.thumbnail} alt={item.title || "thumbnail"} /> : <div className="thumb-empty">NO THUMBNAIL</div>}
              {item.duration > 0 && <span className="duration">{duration(item.duration)}</span>}
              <span className="play">▶</span>
            </button>

            <div className="card-body">
              <h3>{item.title || "(제목 정보 없음)"}</h3>
              <div className="meta">{item.channel || "-"} · {views(item.view_count)} · {date(item.upload_date)}</div>
              {currentBusyText(item) && (
                <div className="status-chip">
                  {currentBusyText(item)}
                  <button type="button" className="chip-cancel" onClick={cancelAction}>✕</button>
                </div>
              )}
              <div className="actions">
                <button type="button" onClick={() => openPreview(item)}>재생</button>
                <button type="button" onClick={() => download(item)} disabled={isBusy("video", item.id)}>
                  {isBusy("video", item.id) ? <><span className="btn-spinner dark" />다운로드 중...</> : "영상"}
                </button>
                <button type="button" onClick={() => subtitles(item)} disabled={isBusy("sub", item.id)}>
                  {isBusy("sub", item.id) ? <><span className="btn-spinner dark" />추출 중...</> : "자막"}
                </button>
                <button type="button" onClick={() => audio(item)} disabled={isBusy("audio", item.id)}>
                  {isBusy("audio", item.id) ? <><span className="btn-spinner dark" />변환 중...</> : "MP3"}
                </button>
              </div>
            </div>
          </article>
        ))}
      </main>}

      {platform !== "translator" && ((platform === "youtube" && hasMore) || (platform !== "youtube" && socialHasMore)) && (
        <div className="load-more">
          <button type="button" onClick={() => search(false)} disabled={loading}>
            {loading ? "더 가져오는 중..." : "더 불러오기"}
          </button>
        </div>
      )}

      {selected && (
        <div className="modal-backdrop" onClick={closePreview}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <div>
                <div className="eyebrow">PREVIEW</div>
                <h2>{selected.title}</h2>
              </div>
              <button type="button" onClick={closePreview}>×</button>
            </div>
            <div className="modal-content">
              {isYoutubeItem(selected) ? (
                <div className={`player-shell${previewReady ? " is-ready" : ""}`}>
                  {!previewReady && (
                    <div className="player-loading" aria-live="polite">
                      <div className="player-spinner" />
                      <span>영상 로딩 중...</span>
                    </div>
                  )}
                  <div className="player">
                    <iframe
                      key={selected.id}
                      src={getYoutubeEmbedSrc(selected.id)}
                      title={selected.title}
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                      loading="eager"
                      referrerPolicy="strict-origin-when-cross-origin"
                      allowFullScreen
                      onLoad={() => setPreviewReady(true)}
                    />
                  </div>
                </div>
              ) : (
                <div className="external-preview">
                  {selected.thumbnail ? <img src={selected.thumbnail} alt={selected.title || "thumbnail"} /> : <div className="thumb-empty">NO PREVIEW</div>}
                  <p>이 플랫폼은 인앱 임베드 대신 원본 링크에서 재생됩니다.</p>
                </div>
              )}

              {currentBusyText(selected) && (
                <div className="modal-status">
                  {currentBusyText(selected)}
                  <button type="button" className="chip-cancel" onClick={cancelAction}>✕ 취소</button>
                </div>
              )}

              <div className="modal-actions">
                <a href={selected.url} target="_blank" rel="noreferrer">원본 보기</a>
                <button type="button" onClick={() => download(selected)} disabled={isBusy("video", selected.id)}>
                  {isBusy("video", selected.id) ? <><span className="btn-spinner dark" />다운로드 중...</> : "영상 저장"}
                </button>
                <button type="button" onClick={() => subtitles(selected)} disabled={isBusy("sub", selected.id)}>
                  {isBusy("sub", selected.id) ? <><span className="btn-spinner dark" />추출 중...</> : "자막 저장"}
                </button>
                <button type="button" onClick={() => audio(selected)} disabled={isBusy("audio", selected.id)}>
                  {isBusy("audio", selected.id) ? <><span className="btn-spinner dark" />변환 중...</> : "MP3 추출"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
