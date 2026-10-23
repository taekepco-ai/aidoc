"""Google Gemini API 클라이언트 (스트리밍 + 모델 폴백 체인).

의존성은 requests 하나. 웹(api/index.py)과 데스크톱(desktop/gen.py)이 공유한다.

모델 폴백
  앞 모델이 할당량 초과·과부하·미존재로 실패하면 다음 모델로 자동 전환한다.
  단 스트리밍 특성상 이미 토큰을 내보낸 뒤에는 전환하지 않는다(출력이 중복되므로).
"""
import json
import os
from pathlib import Path

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"

# 앞에서부터 시도한다. 존재하지 않는 ID가 섞여 있어도 404로 건너뛰므로 안전하다.
# Flash 를 앞에 둔다. Pro 는 생성이 몇 배 느리고 thinking 을 끌 수 없어(THINKING_BUDGET=0
# 이 적용되지 않음) 인수인계처럼 입력·출력이 큰 작업은 수 분씩 걸렸다.
# Pro 는 Flash 가 모두 막혔을 때(할당량·과부하) 쓰는 예비로 남긴다.
DEFAULT_MODEL_CHAIN = [
  'gemini-3.7-flash',
  'gemini-3.6-flash',
  'gemini-3.5-flash',
  'gemini-2.5-flash',
  'gemini-3.1-pro-preview',
  'gemini-3.1-flash-lite',
  'gemini-3.5-flash-lite',
  'gemini-2.5-flash-lite',
]

# 이 프로세스에서 "없는 모델"로 확인된 ID. 같은 인스턴스가 다음 요청을 받을 때
# 매번 404 를 다시 맞으며 큰 입력을 헛보내지 않도록 건너뛴다.
# (할당량·과부하는 일시적이라 기억하지 않는다)
_MISSING_MODELS = set()


def _resolve_chain():
    """GEMINI_MODELS(콤마 구분) > GEMINI_MODEL(선두 고정) > 기본 체인."""
    raw = os.environ.get("GEMINI_MODELS", "").strip()
    if raw:
        chain = [m.strip() for m in raw.split(",") if m.strip()]
        if chain:
            return chain

    chain = list(DEFAULT_MODEL_CHAIN)
    first = os.environ.get("GEMINI_MODEL", "").strip()
    if first:
        chain = [first] + [m for m in chain if m != first]
    return chain


MODEL_CHAIN = _resolve_chain()
DEFAULT_MODEL = MODEL_CHAIN[0]

# 클라우드 모델은 로컬 SLM과 토큰 예산이 달라 문서가 중간에 잘리기 쉬움.
# 호출부의 max_new_tokens(600/900) 대신 이 값을 하한으로 보장한다.
MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "4096"))

# 2.5 이상은 기본적으로 thinking이 켜져 있음.
#   0  = 끄고 즉시 응답 (기본값, 응답 지연 최소화)
#  -1  = 동적 thinking (모델이 알아서 판단)
#  >0  = 해당 토큰 예산만큼 thinking
THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))

# 양식·문체를 엄격히 강제하는 프롬프트라 낮은 temperature가 유리하다.
DEFAULT_TEMPERATURE = float(os.environ.get("GEMINI_TEMPERATURE", "0.3"))
DEFAULT_TOP_P = float(os.environ.get("GEMINI_TOP_P", "0.9"))
DEFAULT_TOP_K = int(os.environ.get("GEMINI_TOP_K", "0"))

# 다음 모델로 넘어갈 상태 코드 (할당량·과부하·모델 미존재)
_RETRYABLE_STATUS = {404, 429, 500, 502, 503, 504}

# 400이면서 아래 문구가 있으면 "그 모델을 못 쓴다"는 뜻이므로 다음 모델로 넘어간다.
# 그 외 400은 우리 요청이 잘못된 것이므로 즉시 실패시킨다.
_MODEL_UNAVAILABLE_HINTS = (
    "not found", "not supported", "unsupported", "does not exist",
    "is not available", "invalid model",
)


class GeminiError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 한국어 오류 메시지."""


class _Retryable(Exception):
    """다음 모델로 넘어가야 하는 실패."""


def load_api_key(key_file: Path | None = None) -> str:
    """GEMINI_API_KEY → GEMINI_KEY → key_file 순으로 조회."""
    for name in ("GEMINI_API_KEY", "GEMINI_KEY"):
        key = os.environ.get(name, "").strip()
        if key:
            return key
    if key_file and key_file.exists():
        try:
            return key_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


class GeminiClient:
    def __init__(self, api_key: str, models=None):
        if isinstance(models, str):
            models = [models]
        self.models = list(models) if models else list(MODEL_CHAIN)
        self.api_key = api_key
        self.last_output_tokens = None
        self.last_model = None
        # check()가 실제 사용 가능하다고 확인한 목록 (없으면 self.models 전체를 시도)
        self.verified_models = None

    # ---------- 조회 / 사전 점검 ----------
    def _headers(self):
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def list_models(self):
        """계정에서 실제로 쓸 수 있는 모델 ID 집합."""
        import requests
        names, token = set(), None
        for _ in range(10):  # 페이지네이션 안전장치
            url = f"{ENDPOINT}/models?pageSize=200"
            if token:
                url += f"&pageToken={token}"
            r = requests.get(url, headers=self._headers(), timeout=15)
            if r.status_code in (401, 403):
                raise GeminiError(
                    f"API 키 인증 실패({r.status_code}). 키를 확인하세요.")
            if not r.ok:
                raise GeminiError(self._format_http_error(r))
            body = r.json()
            for m in body.get("models") or []:
                names.add(m.get("name", "").split("/", 1)[-1])
            token = body.get("nextPageToken")
            if not token:
                break
        return names

    def check(self, log=None):
        """키 유효성과 모델 체인 가용성 확인. 치명적 문제만 예외를 던진다."""
        def _log(msg):
            if log:
                log(msg)

        if not self.api_key:
            raise GeminiError(
                "Gemini API 키가 없습니다. 환경변수 GEMINI_API_KEY를 설정하세요.")

        try:
            available = self.list_models()
        except GeminiError:
            raise
        except Exception as e:
            _log(f"모델 목록 조회 실패(무시하고 진행): {e}")
            return

        usable = [m for m in self.models if m in available]
        self.verified_models = usable or None

        if not usable:
            _log("경고: 설정한 모델 중 사용 가능한 것이 없습니다. "
                 "요청 시 순서대로 시도합니다.")
        elif len(usable) == 1:
            _log(f"Gemini 준비 완료. ({usable[0]})")
        else:
            _log(f"Gemini 준비 완료. ({usable[0]}, 예비 {len(usable) - 1}개)")

    def _chain(self):
        chain = self.verified_models or self.models
        # 모두 없는 모델로 기억돼 있으면(키 교체 등) 원래 목록으로 다시 시도한다
        return [m for m in chain if m not in _MISSING_MODELS] or chain

    # ---------- 요청 조립 ----------
    @staticmethod
    def build_payload(messages, max_output_tokens, temperature, top_p, top_k):
        system_parts, contents = [], []
        for m in messages:
            text = m.get("content", "")
            if not text:
                continue
            if m.get("role") == "system":
                system_parts.append({"text": text})
            else:
                contents.append({
                    "role": "model" if m.get("role") == "assistant" else "user",
                    "parts": [{"text": text}],
                })

        gen_config = {
            "temperature": temperature,
            "topP": top_p,
            "maxOutputTokens": max_output_tokens,
            "thinkingConfig": {"thinkingBudget": THINKING_BUDGET},
        }
        if top_k and top_k > 0:
            gen_config["topK"] = top_k

        payload = {"contents": contents, "generationConfig": gen_config}
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        return payload

    # ---------- 스트리밍 생성 ----------
    def stream(self, messages, max_output_tokens=None,
               temperature=DEFAULT_TEMPERATURE, top_p=DEFAULT_TOP_P,
               top_k=DEFAULT_TOP_K, should_stop=None, on_fallback=None):
        """텍스트 조각을 순서대로 yield 한다.

        should_stop:  인자 없는 콜러블. True를 반환하면 중간에 멈춘다.
        on_fallback:  (실패한 모델, 사유)를 받는 콜백. 폴백 발생 시 호출된다.
        """
        self.last_output_tokens = None
        self.last_model = None

        limit = max(max_output_tokens or 0, MAX_OUTPUT_TOKENS)
        payload = self.build_payload(messages, limit, temperature, top_p, top_k)

        chain = self._chain()
        last_error = None

        for model in chain:
            emitted = False
            try:
                for text in self._stream_one(model, payload, should_stop):
                    emitted = True
                    yield text
                self.last_model = model
                return
            except _Retryable as e:
                if emitted:
                    # 이미 화면에 출력된 뒤라 다른 모델로 갈아타면 내용이 겹친다.
                    raise GeminiError(f"{model} 응답 중 오류가 발생했습니다: {e}")
                last_error = f"{model} → {e}"
                if on_fallback:
                    on_fallback(model, str(e))
                continue

        raise GeminiError(
            "사용 가능한 모델이 없습니다. 마지막 오류: " + (last_error or "알 수 없음"))

    def _stream_one(self, model, payload, should_stop):
        import requests

        url = f"{ENDPOINT}/models/{model}:streamGenerateContent?alt=sse"
        try:
            resp = requests.post(url, headers=self._headers(), json=payload,
                                 stream=True, timeout=(15, 600))
        except Exception as e:
            raise _Retryable(f"연결 실패: {e}")

        with resp as r:
            if not r.ok:
                if r.status_code == 404:
                    _MISSING_MODELS.add(model)
                self._raise_for_status(r)

            # SSE 응답에는 charset 이 없어서 requests 가 ISO-8859-1 로 간주한다.
            # 명시하지 않으면 한글이 전부 깨진다.
            r.encoding = "utf-8"

            for line in r.iter_lines(decode_unicode=True):
                if should_stop and should_stop():
                    return
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if not body or body == "[DONE]":
                    continue
                try:
                    obj = json.loads(body)
                except Exception:
                    continue

                if "error" in obj:
                    raise GeminiError(
                        obj["error"].get("message", str(obj["error"])))

                block = (obj.get("promptFeedback") or {}).get("blockReason")
                if block:
                    raise GeminiError(f"요청이 안전 필터에 차단되었습니다: {block}")

                usage = obj.get("usageMetadata") or {}
                if usage.get("candidatesTokenCount"):
                    self.last_output_tokens = usage["candidatesTokenCount"]

                for cand in obj.get("candidates") or []:
                    for part in (cand.get("content") or {}).get("parts") or []:
                        # thinking 요약 파트는 결과에 포함하지 않는다
                        if part.get("thought"):
                            continue
                        text = part.get("text", "")
                        if text:
                            yield text
                    reason = cand.get("finishReason")
                    if reason and reason not in ("STOP", "MAX_TOKENS"):
                        raise GeminiError(f"생성이 중단되었습니다: {reason}")

    # ---------- 오류 분류 ----------
    @classmethod
    def _raise_for_status(cls, resp):
        """다음 모델로 넘어갈 실패는 _Retryable, 그 외는 GeminiError."""
        status = resp.status_code
        msg = cls._error_message(resp)

        # 키 문제는 모델을 바꿔도 동일하므로 즉시 실패
        if status in (401, 403):
            raise GeminiError(f"API 키 인증 실패({status}). {msg}")

        if status in _RETRYABLE_STATUS:
            raise _Retryable(f"HTTP {status} {msg}".strip())

        # 400은 "그 모델을 못 쓴다"일 때만 넘어간다. 나머지는 우리 요청 문제.
        if status == 400:
            low = msg.lower()
            if any(h in low for h in _MODEL_UNAVAILABLE_HINTS):
                raise _Retryable(f"HTTP 400 {msg}")
            raise GeminiError(f"요청이 거부되었습니다(400). {msg}")

        raise GeminiError(f"Gemini API 오류 {status}: {msg}")

    @staticmethod
    def _error_message(resp):
        try:
            return resp.json().get("error", {}).get("message", "") or ""
        except Exception:
            resp.encoding = resp.encoding or "utf-8"
            return (resp.text or "")[:300]

    @classmethod
    def _format_http_error(cls, resp):
        msg = cls._error_message(resp)
        if resp.status_code in (401, 403):
            return f"API 키 인증 실패({resp.status_code}). {msg}"
        if resp.status_code == 429:
            return f"요청 한도 초과(429). 잠시 후 재시도하세요. {msg}"
        if resp.status_code >= 500:
            return f"Gemini 서버 오류({resp.status_code}). 잠시 후 재시도하세요. {msg}"
        return f"Gemini API 오류 {resp.status_code}: {msg}"
