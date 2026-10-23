"""공문 검토·작성 시스템 — 데스크톱 (Tkinter)

백엔드 3종을 지원한다 (SLM_BACKEND=gemini|api|onnx, 미지정 시 자동 선택).
  gemini — Google Gemini API          (권장)
  api    — 원격 SLM 서버 (SLM_API_URL)
  onnx   — 로컬 EXAONE-3.5-2.4B ONNX

프롬프트·후처리·파일 추출은 core/ 에 있고 웹(api/index.py)과 공유한다.
"""
import os
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, scrolledtext, filedialog, messagebox

np = None
ort = None
AutoTokenizer = None

HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE.parent / "models" / "onnx-community" / "EXAONE-3.5-2.4B-Instruct"
ONNX_PATH = MODEL_DIR / "onnx" / "model_q4.onnx"

NUM_LAYERS = 30
NUM_KV_HEADS = 8
HEAD_DIM = 80

# ============================================================
# 공용 모듈 (core/) — 프롬프트·후처리·파일 추출을 웹과 공유한다
# ============================================================
sys.path.insert(0, str(HERE.parent))

from core import gemini, hwpx, orgs, prompts               # noqa: E402
from core.extract import extract_from_path                 # noqa: E402

DOC_TYPES = prompts.DOC_TYPES
_strip_markdown = prompts.strip_markdown
_ensure_end_mark = prompts.ensure_end_mark


def _extract_text_from_file(path: str) -> str:
    return extract_from_path(path)


# ============================================================
# 모델
# ============================================================
API_URL = os.environ.get("SLM_API_URL", "").rstrip("/")

GEMINI_KEY_FILE = HERE / "gemini_key.txt"

# 로컬 SLM(EXAONE 2.4B) 기준으로 잡혀 있던 샘플링 값.
# Gemini는 core.gemini의 기본값(양식 준수에 유리한 낮은 temperature)을 쓴다.
ONNX_SAMPLING = (0.7, 0.8, 20)


def _load_gemini_key() -> str:
    return gemini.load_api_key(GEMINI_KEY_FILE)


def _save_gemini_key(key: str) -> None:
    GEMINI_KEY_FILE.write_text(key.strip() + "\n", encoding="utf-8")


class GeminiGenerator:
    """core.gemini.GeminiClient를 데스크톱 인터페이스에 맞춘 어댑터."""

    def __init__(self, api_key, models=None):
        # models=None 이면 core.gemini의 폴백 체인 전체를 사용한다
        self.client = gemini.GeminiClient(api_key, models)
        self.last_output_tokens = None
        self.last_model = None
        self.log = None
        self.sampling = (gemini.DEFAULT_TEMPERATURE,
                         gemini.DEFAULT_TOP_P,
                         gemini.DEFAULT_TOP_K)

    def load(self, log):
        self.log = log
        self.client.check(log)

    def generate(self, messages, max_new_tokens, temperature, top_p, top_k,
                 on_token=None, stop_event=None):
        produced = []
        stop = (lambda: stop_event.is_set()) if stop_event else None

        def _on_fallback(model, reason):
            if self.log:
                self.log(f"{model} 사용 불가 → 다음 모델로 전환 ({reason})")

        for chunk in self.client.stream(
            messages, max_output_tokens=max_new_tokens,
            temperature=temperature, top_p=top_p, top_k=top_k,
            should_stop=stop, on_fallback=_on_fallback,
        ):
            produced.append(chunk)
            if on_token:
                on_token(chunk)
        self.last_output_tokens = self.client.last_output_tokens
        self.last_model = self.client.last_model
        return produced


class ApiGenerator:
    def __init__(self, base_url):
        self.base_url = base_url

    def load(self, log):
        log(f"원격 서버 확인 중... {self.base_url}")
        import requests
        try:
            r = requests.get(self.base_url + "/health", timeout=10)
            r.raise_for_status()
            if r.json().get("ready"):
                log("원격 서버 준비됨.")
                return
            log(f"원격 서버 워밍업 중... ({r.json().get('status')})")
        except Exception as e:
            log(f"원격 서버 응답 없음: {e}")
        import requests as _rq
        for _ in range(60):
            try:
                r = _rq.get(self.base_url + "/health", timeout=10)
                if r.ok and r.json().get("ready"):
                    log("원격 서버 준비됨.")
                    return
                log(f"기다리는 중... {r.json().get('status','?')}")
            except Exception:
                log("연결 시도 중...")
            import time as _t; _t.sleep(5)
        raise RuntimeError("원격 서버 준비 안 됨 (5분 타임아웃)")

    def generate(self, messages, max_new_tokens, temperature, top_p, top_k,
                 on_token=None, stop_event=None):
        import json as _json, requests
        produced = []
        with requests.post(
            self.base_url + "/generate",
            json={"messages": messages, "max_new_tokens": max_new_tokens,
                  "temperature": temperature, "top_p": top_p, "top_k": top_k},
            stream=True, timeout=(10, 600),
            headers={"Accept": "text/event-stream"},
        ) as r:
            r.raise_for_status()
            # SSE 응답에 charset 이 없으면 requests 가 ISO-8859-1 로 간주해 한글이 깨진다
            r.encoding = "utf-8"
            for line in r.iter_lines(decode_unicode=True):
                if stop_event and stop_event.is_set(): break
                if not line or not line.startswith("data:"): continue
                try:
                    obj = _json.loads(line[5:].strip())
                except Exception:
                    continue
                if "error" in obj: raise RuntimeError(obj["error"])
                if obj.get("done"): break
                tok = obj.get("token", "")
                if tok:
                    produced.append(tok)
                    if on_token: on_token(tok)
        return produced


class Generator:
    def __init__(self):
        self.tokenizer = None
        self.session = None
        self.output_index = {}

    def load(self, log):
        global np, ort, AutoTokenizer
        log("라이브러리 로딩 중...")
        import numpy as _np, onnxruntime as _ort
        from transformers import AutoTokenizer as _AT
        np, ort, AutoTokenizer = _np, _ort, _AT

        log("토크나이저 로딩 중...")
        self.tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR), trust_remote_code=True)

        log("세션 생성 중...")
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = max(1, (os.cpu_count() or 4) - 1)
        self.session = ort.InferenceSession(
            str(ONNX_PATH), sess_options=so, providers=["CPUExecutionProvider"])
        self.output_index = {o.name: i for i, o in enumerate(self.session.get_outputs())}
        log("모델 준비 완료.")

    def _sample(self, logits, temperature, top_p, top_k):
        logits = logits.astype(np.float32)
        if temperature <= 0: return int(np.argmax(logits))
        logits = logits / temperature
        if top_k > 0 and top_k < logits.size:
            kth = np.partition(logits, -top_k)[-top_k]
            logits = np.where(logits < kth, -np.inf, logits)
        probs = np.exp(logits - logits.max()); probs /= probs.sum()
        if top_p < 1.0:
            order = np.argsort(-probs); sorted_p = probs[order]; cum = np.cumsum(sorted_p)
            cutoff = np.searchsorted(cum, top_p) + 1
            keep = order[:cutoff]; new_p = np.zeros_like(probs); new_p[keep] = probs[keep]
            probs = new_p / new_p.sum()
        return int(np.random.choice(len(probs), p=probs))

    def generate(self, messages, max_new_tokens, temperature, top_p, top_k,
                 on_token=None, stop_event=None):
        tok = self.tokenizer
        ids = tok.apply_chat_template(messages, return_tensors="np",
                                      add_generation_prompt=True).astype(np.int64)
        input_ids = ids; seq_len = input_ids.shape[1]
        attention_mask = np.ones((1, seq_len), dtype=np.int64)
        position_ids = np.arange(seq_len, dtype=np.int64).reshape(1, -1)

        past_kv = {}
        for i in range(NUM_LAYERS):
            past_kv[f"past_key_values.{i}.key"] = np.zeros((1, NUM_KV_HEADS, 0, HEAD_DIM), dtype=np.float32)
            past_kv[f"past_key_values.{i}.value"] = np.zeros((1, NUM_KV_HEADS, 0, HEAD_DIM), dtype=np.float32)

        eos_id = tok.eos_token_id; produced = []; total_len = seq_len

        for _ in range(max_new_tokens):
            if stop_event and stop_event.is_set(): break
            feeds = {"input_ids": input_ids, "attention_mask": attention_mask,
                     "position_ids": position_ids, **past_kv}
            outputs = self.session.run(None, feeds)
            logits = outputs[self.output_index["logits"]]
            next_id = self._sample(logits[0, -1, :], temperature, top_p, top_k)
            if next_id == eos_id: break
            produced.append(next_id)
            if on_token: on_token(tok.decode([next_id], skip_special_tokens=True))
            input_ids = np.array([[next_id]], dtype=np.int64)
            total_len += 1
            attention_mask = np.ones((1, total_len), dtype=np.int64)
            position_ids = np.array([[total_len - 1]], dtype=np.int64)
            past_kv = {}
            for i in range(NUM_LAYERS):
                past_kv[f"past_key_values.{i}.key"] = outputs[self.output_index[f"present.{i}.key"]]
                past_kv[f"past_key_values.{i}.value"] = outputs[self.output_index[f"present.{i}.value"]]
        return produced


# ============================================================
# Tkinter UI
# ============================================================
try:
    ORGS = orgs.load_orgs()
except Exception:
    ORGS = []
ORG_NAMES = [o["name"] for o in ORGS]


# 백엔드 선택: SLM_BACKEND=gemini|api|onnx (미지정 시 자동)
BACKEND = os.environ.get("SLM_BACKEND", "").strip().lower()


def _resolve_backend(gemini_key: str) -> str:
    if BACKEND:
        return BACKEND
    if gemini_key:
        return "gemini"
    if API_URL:
        return "api"
    return "onnx"


def _make_generator(backend: str, gemini_key: str):
    if backend == "gemini":
        return GeminiGenerator(gemini_key)
    if backend == "api":
        if not API_URL:
            raise RuntimeError("SLM_BACKEND=api 인데 SLM_API_URL 환경변수가 없습니다.")
        return ApiGenerator(API_URL)
    return Generator()


class App(tk.Tk):
    def __init__(self, generator=None):
        super().__init__()
        self.title("공문 검토·작성 시스템")
        self.geometry("1100x820")

        self.gen = generator or (ApiGenerator(API_URL) if API_URL else Generator())
        self.stop_event = threading.Event()
        self.ui_queue = queue.Queue()
        self.is_busy = False
        self._gen_buffer = ""
        self._task_mode = "write"
        # 불러온 .hwpx 는 저장할 때 서식 원본으로 다시 쓴다
        self._template_path = None

        self._build_ui()
        self.after(100, self._pump_queue)
        threading.Thread(target=self._load_worker, daemon=True).start()

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        top = ttk.Frame(self); top.pack(side=tk.TOP, fill=tk.X, **pad)

        # 기관 선택
        row0 = ttk.Frame(top); row0.pack(fill=tk.X, pady=4)
        ttk.Label(row0, text="기관", width=10).pack(side=tk.LEFT)
        self.org_name = tk.StringVar(value=ORG_NAMES[0] if ORG_NAMES else "")
        org_box = ttk.Combobox(row0, textvariable=self.org_name,
                               values=ORG_NAMES, state="readonly", width=28)
        org_box.pack(side=tk.LEFT)
        if not ORG_NAMES:
            org_box.config(state=tk.DISABLED)
            ttk.Label(row0, text="  core/orgs.json 을 읽지 못했습니다.",
                      foreground="#c0392b").pack(side=tk.LEFT)
        else:
            ttk.Label(row0, text="  기관 추가는 core/orgs.json 편집",
                      foreground="#777").pack(side=tk.LEFT)

        # 문서 양식 선택
        row1 = ttk.Frame(top); row1.pack(fill=tk.X, pady=4)
        ttk.Label(row1, text="문서 양식", width=10).pack(side=tk.LEFT)
        self.doc_mode = tk.StringVar(value=DOC_TYPES[0])
        for val in DOC_TYPES:
            ttk.Radiobutton(row1, text=val, variable=self.doc_mode, value=val).pack(
                side=tk.LEFT, padx=(0, 14))

        # 탭
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.X, padx=10, pady=(0, 4))

        # 검토 탭 (첫 번째)
        rf = ttk.Frame(self.notebook)
        self.notebook.add(rf, text="  검토  ")
        file_row = ttk.Frame(rf); file_row.pack(fill=tk.X, padx=6, pady=(6, 2))
        ttk.Label(file_row,
                  text="원문 입력  (공문·보고서를 붙여넣거나 파일을 불러오세요)").pack(side=tk.LEFT)
        ttk.Button(file_row, text="파일 불러오기...",
                   command=self.on_open_file).pack(side=tk.RIGHT)
        self.review_prompt = scrolledtext.ScrolledText(rf, height=12, wrap=tk.WORD,
                                                       font=("맑은 고딕", 10))
        self.review_prompt.pack(fill=tk.X, padx=6, pady=(0, 6))

        # 작성 탭 (두 번째)
        wf = ttk.Frame(self.notebook)
        self.notebook.add(wf, text="  작성  ")
        ttk.Label(wf, text="내용 요점  (작성할 내용의 핵심 사항을 입력하세요)").pack(
            anchor=tk.W, padx=6, pady=(6, 2))
        self.write_prompt = scrolledtext.ScrolledText(wf, height=8, wrap=tk.WORD,
                                                      font=("맑은 고딕", 10))
        self.write_prompt.pack(fill=tk.X, padx=6, pady=(0, 6))

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_change)

        # 버튼 행
        btnrow = ttk.Frame(self); btnrow.pack(fill=tk.X, padx=10, pady=(0, 4))
        self.gen_btn = ttk.Button(btnrow, text="검토", command=self.on_generate,
                                  state=tk.DISABLED)
        self.gen_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(btnrow, text="중지", command=self.on_stop,
                                   state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btnrow, text="복사", command=self.on_copy).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btnrow, text="파일로 저장...", command=self.on_save).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btnrow, text="지우기", command=self.on_clear).pack(side=tk.LEFT, padx=(6, 0))
        self.status_var = tk.StringVar(value="로딩 중... 잠시만 기다려주세요.")
        ttk.Label(btnrow, textvariable=self.status_var, foreground="#555").pack(side=tk.RIGHT)

        # 결과
        ttk.Label(self, text="결과").pack(anchor=tk.W, padx=10)
        self.output = scrolledtext.ScrolledText(
            self, wrap=tk.WORD,
            font=("D2Coding", 11) if self._has_font("D2Coding") else ("맑은 고딕", 11))
        self.output.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    def _on_tab_change(self, _event=None):
        if not self.is_busy:
            self.gen_btn.config(text=self._run_button_text())

    def _current_tab(self):
        # 탭 순서: 0=검토, 1=작성
        return "review" if self.notebook.index(self.notebook.select()) == 0 else "write"

    def _run_button_text(self):
        return "검토" if self._current_tab() == "review" else "생성"

    def _current_org(self):
        name = self.org_name.get()
        for org in ORGS:
            if org["name"] == name:
                return org
        if ORGS:
            return ORGS[0]
        raise RuntimeError("등록된 기관이 없습니다. core/orgs.json 을 확인하세요.")

    def _has_font(self, name):
        try:
            import tkinter.font as tkfont
            return name in tkfont.families()
        except Exception:
            return False

    # ---------- 큐 ----------
    def _pump_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "ready":
                    self.gen_btn.config(state=tk.NORMAL)
                    self.status_var.set("준비 완료. 입력 후 버튼을 누르세요.")
                elif kind == "token":
                    self._gen_buffer += payload
                    self.output.delete("1.0", tk.END)
                    self.output.insert(tk.END, _strip_markdown(self._gen_buffer))
                    self.output.see(tk.END)
                elif kind == "done":
                    stripped = _strip_markdown(self._gen_buffer)
                    # 공문 작성 모드에서만 끝. 자동 추가
                    if self._task_mode == "write" and "공문" in self.doc_mode.get():
                        final = _ensure_end_mark(stripped)
                    else:
                        final = stripped
                    self.output.delete("1.0", tk.END)
                    self.output.insert(tk.END, final)
                    self.output.see(tk.END)
                    self.is_busy = False
                    self.gen_btn.config(state=tk.NORMAL,
                                        text=self._run_button_text())
                    self.stop_btn.config(state=tk.DISABLED)
                    self.status_var.set(payload)
                elif kind == "error":
                    self.is_busy = False
                    self.gen_btn.config(state=tk.NORMAL,
                                        text=self._run_button_text())
                    self.stop_btn.config(state=tk.DISABLED)
                    self.status_var.set("오류: " + payload)
        except queue.Empty:
            pass
        self.after(80, self._pump_queue)

    def _push(self, kind, payload=""):
        self.ui_queue.put((kind, payload))

    def _load_worker(self):
        try:
            self.gen.load(lambda s: self._push("status", s))
            self._push("ready", "")
        except Exception as e:
            self._push("error", f"모델 로딩 실패: {e}")

    # ---------- 타입 선택 다이얼로그 (검토 전용) ----------
    def _ask_doc_type_dialog(self) -> str | None:
        chosen = tk.StringVar(value="")
        dlg = tk.Toplevel(self)
        dlg.title("문서 유형 선택")
        dlg.geometry("300x200")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="검토할 문서 유형을 선택하세요.",
                  font=("맑은 고딕", 11, "bold")).pack(pady=(18, 10))

        for label, val in [
            ("공문 — 외부", "공문-외부"),
            ("공문 — 내부",     "공문-내부"),
            ("보고서",          "보고서"),
        ]:
            ttk.Button(
                dlg, text=label, width=24,
                command=lambda v=val: (chosen.set(v), dlg.destroy()),
            ).pack(pady=3)

        ttk.Button(dlg, text="취소", command=dlg.destroy).pack(pady=(8, 0))
        self.wait_window(dlg)
        return chosen.get() or None

    # ---------- 핸들러 ----------
    def on_generate(self):
        if self.is_busy: return
        tab = self._current_tab()

        if tab == "write":
            user_prompt = self.write_prompt.get("1.0", tk.END).strip()
            if not user_prompt:
                self.status_var.set("내용 요점을 입력해 주세요."); return
            sys_prompt = prompts.get_system_prompt(
                "write", self.doc_mode.get(), self._current_org())
            max_tokens = 600; action = "생성 중..."
        else:
            user_prompt = self.review_prompt.get("1.0", tk.END).strip()
            if not user_prompt:
                self.status_var.set("검토할 원문을 입력해 주세요."); return
            doc_type = self._ask_doc_type_dialog()
            if doc_type is None: return
            sys_prompt = prompts.get_system_prompt(
                "review", doc_type, self._current_org())
            max_tokens = 900; action = f"검토 중... ({doc_type})"

        self._task_mode = tab
        self.output.delete("1.0", tk.END)
        self._gen_buffer = ""
        self.is_busy = True
        self.stop_event.clear()
        self.gen_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_var.set(action)
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user",   "content": user_prompt}]
        threading.Thread(
            target=self._generate_worker, args=(messages, max_tokens), daemon=True
        ).start()

    def _generate_worker(self, messages, max_new_tokens):
        try:
            import time
            t0 = time.time()
            # 백엔드마다 적정 샘플링 값이 다르다 (ONNX_SAMPLING 주석 참고)
            temperature, top_p, top_k = getattr(self.gen, "sampling", ONNX_SAMPLING)
            tokens = self.gen.generate(
                messages, max_new_tokens=max_new_tokens,
                temperature=temperature, top_p=top_p, top_k=top_k,
                on_token=lambda t: self._push("token", t),
                stop_event=self.stop_event,
            )
            elapsed = time.time() - t0
            # Gemini는 청크 단위 스트리밍이라 usageMetadata의 실제 토큰 수를 우선 사용
            n_tokens = getattr(self.gen, "last_output_tokens", None) or len(tokens)
            tps = n_tokens / elapsed if elapsed > 0 else 0
            used = getattr(self.gen, "last_model", None)
            suffix = f", {used}" if used else ""
            self._push(
                "done",
                f"완료 ({n_tokens} 토큰, {elapsed:.1f}초, {tps:.1f} tok/s{suffix})")
        except Exception as e:
            self._push("error", str(e))

    def on_stop(self):
        if self.is_busy:
            self.stop_event.set()
            self.status_var.set("중지 중...")

    def on_copy(self):
        text = self.output.get("1.0", tk.END).rstrip()
        if text:
            self.clipboard_clear(); self.clipboard_append(text)
            self.status_var.set("클립보드에 복사됨.")

    def on_clear(self):
        self.output.delete("1.0", tk.END)

    def on_open_file(self):
        path = filedialog.askopenfilename(
            title="검토할 문서 파일 선택",
            filetypes=[
                ("문서 파일", "*.hwp *.hwpx *.docx *.pdf *.txt"),
                ("HWP 파일", "*.hwp"), ("HWPX 파일", "*.hwpx"),
                ("Word 문서", "*.docx"), ("PDF 파일", "*.pdf"),
                ("텍스트 파일", "*.txt"), ("모든 파일", "*.*"),
            ],
        )
        if not path: return
        try:
            self.status_var.set(f"파일 읽는 중: {os.path.basename(path)}")
            self.update_idletasks()
            text = _extract_text_from_file(path)
            self._template_path = path if path.lower().endswith(".hwpx") else None
            self.review_prompt.delete("1.0", tk.END)
            self.review_prompt.insert(tk.END, text)
            self.status_var.set(f"로드 완료: {os.path.basename(path)}")
        except Exception as e:
            self.status_var.set(f"파일 로드 실패: {e}")
            messagebox.showerror("파일 로드 실패", str(e))

    def on_save(self):
        text = self.output.get("1.0", tk.END).rstrip()
        if not text:
            self.status_var.set("저장할 내용이 없습니다."); return
        text = _strip_markdown(text)
        if self._task_mode == "write" and "공문" in self.doc_mode.get():
            text = _ensure_end_mark(text)

        m = re.search(r"^제\s*목\s+(.+)$", text, re.MULTILINE)
        title = m.group(1).strip() if m else self.doc_mode.get()
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:50]

        path = filedialog.asksaveasfilename(
            title="문서 저장", initialfile=safe_title,
            defaultextension=".hwpx",
            filetypes=[("한글 문서 (.hwpx)", "*.hwpx")],
        )
        if not path: return
        try:
            with open(path, "wb") as f:
                f.write(hwpx.build_hwpx(text, safe_title, self._template_path))
            self.status_var.set(f"저장 완료: {os.path.basename(path)}")
        except Exception as e:
            import traceback
            messagebox.showerror("저장 실패",
                                 f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[:1000]}")
            self.status_var.set("저장 실패 (상세는 팝업 참조)")


def _show_error_dialog(msg: str):
    try:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("공문 검토·작성 시스템 - 오류", msg)
        root.destroy()
    except Exception:
        try:
            with open(HERE / "error.log", "w", encoding="utf-8") as f:
                f.write(msg)
        except Exception:
            pass


def _prompt_gemini_key() -> str:
    """키가 없을 때 입력 다이얼로그를 띄우고, 입력받으면 파일에 저장한다."""
    from tkinter import simpledialog
    root = tk.Tk()
    root.withdraw()
    try:
        key = simpledialog.askstring(
            "Gemini API 키 입력",
            "Google AI Studio에서 발급받은 API 키를 입력하세요.\n"
            f"입력한 키는 {GEMINI_KEY_FILE.name} 파일에 저장됩니다.",
            show="*", parent=root,
        )
    finally:
        root.destroy()

    key = (key or "").strip()
    if key:
        try:
            _save_gemini_key(key)
        except Exception as e:
            _show_error_dialog(f"API 키 저장 실패: {e}")
    return key


def main():
    try:
        gemini_key = _load_gemini_key()
        backend = _resolve_backend(gemini_key)

        if backend == "gemini" and not gemini_key:
            gemini_key = _prompt_gemini_key()
            if not gemini_key:
                return
        elif backend == "onnx" and not ONNX_PATH.exists():
            _show_error_dialog(
                f"모델 파일을 찾을 수 없습니다:\n{ONNX_PATH}\n\n"
                "models/onnx-community/EXAONE-3.5-2.4B-Instruct/ 폴더와 그 안의 "
                "onnx/model_q4.onnx, model_q4.onnx_data 파일이 있어야 합니다.\n\n"
                "또는 GEMINI_API_KEY(권장) 나 SLM_API_URL 환경변수를 설정하세요."
            )
            return

        App(_make_generator(backend, gemini_key)).mainloop()
    except Exception as e:
        import traceback
        _show_error_dialog(f"예기치 못한 오류가 발생했습니다.\n\n{e}\n\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
