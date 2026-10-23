"""한전 공문 검토·작성 시스템 — 웹 백엔드 (Vercel Python 런타임).

엔드포인트
  GET  /api/health    상태 확인
  POST /api/extract   문서 파일 업로드 → 텍스트
  POST /api/generate  작성/검토/요약/인수인계 (SSE 스트리밍)

환경 변수
  GEMINI_API_KEY  (필수)  Vercel 프로젝트 Environment Variables에 등록
  GEMINI_MODEL            기본 gemini-2.5-flash
"""
import json
import sys
import time
from pathlib import Path

# Vercel 번들에서 core/ 를 import 할 수 있게 프로젝트 루트를 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from core import prompts, related, store
from core.extract import HANDOVER_EXTS, SUPPORTED_EXTS, ExtractError, extract_from_bytes
from core.gemini import MODEL_CHAIN, GeminiClient, GeminiError, load_api_key
from core.hwpx import HwpxError, build_hwpx
from core.orgs import OrgError, load_orgs, normalize_org, resolve_org

# Vercel 요청 본문 상한이 4.5MB라 그보다 낮게 잡는다
MAX_UPLOAD_BYTES = 4 * 1024 * 1024

app = FastAPI(title="한전 공문 검토·작성 시스템", version="1.1")


def _client() -> GeminiClient:
    key = load_api_key()
    if not key:
        raise HTTPException(
            500,
            "서버에 GEMINI_API_KEY가 설정되지 않았습니다. "
            "Vercel 프로젝트 환경 변수를 확인하세요.",
        )
    return GeminiClient(key)


# ============================================================
# 상태
# ============================================================
@app.get("/api/health")
def health():
    return {
        "ok": bool(load_api_key()),
        "model": MODEL_CHAIN[0],
        "models": MODEL_CHAIN,
        "doc_types": prompts.DOC_TYPES,
        "supported_exts": list(SUPPORTED_EXTS),
        "handover_exts": list(HANDOVER_EXTS),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "store_enabled": store.enabled(),
    }


@app.post("/api/hwpx")
async def to_hwpx(
    text: str = Form(...),
    title: str = Form("문서"),
    template: UploadFile | None = File(None),
):
    """결과 텍스트를 한글(.hwpx) 문서로 변환해 내려준다.

    template 에 사용자가 업로드했던 .hwpx 를 함께 보내면 그 파일의 글꼴·여백·
    용지 설정을 그대로 물려받고 본문만 교체한다.
    """
    if not text.strip():
        raise HTTPException(400, "저장할 내용이 없습니다.")

    template_bytes = None
    if template is not None:
        raw = await template.read()
        if raw and len(raw) <= MAX_UPLOAD_BYTES:
            template_bytes = raw

    try:
        data = build_hwpx(text, title, template_bytes)
    except HwpxError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"HWPX 생성 실패: {e}")

    return Response(
        content=data,
        media_type="application/hwp+zip",
        headers={"Content-Disposition": 'attachment; filename="document.hwpx"'},
    )


class FormatRequest(BaseModel):
    doc_type: str = Field(..., description="공문-외부 | 공문-내부 | 보고서")
    org: object | None = Field(None, description="기관 id 또는 기관 객체")
    text: str = Field("", description="양식 정의 (마크다운)")


@app.get("/api/format")
def get_format(doc_type: str, org: str | None = None):
    """기관·유형별 양식 정의를 읽는다."""
    try:
        resolved = resolve_org(org)
    except OrgError as e:
        raise HTTPException(400, str(e))
    if doc_type not in prompts.DOC_TYPES:
        raise HTTPException(400, f"알 수 없는 문서 유형: {doc_type}")

    return {
        "org": resolved["name"],
        "doc_type": doc_type,
        "path": f"datas/{resolved['id']}/{doc_type}/{store.FORMAT_FILENAME}",
        "text": store.load_format(resolved["id"], doc_type) or "",
    }


@app.put("/api/format")
def put_format(req: FormatRequest):
    """기관·유형별 양식 정의를 저장한다. 재배포 없이 다음 요청부터 반영된다."""
    try:
        resolved = resolve_org(req.org)
    except OrgError as e:
        raise HTTPException(400, str(e))
    if req.doc_type not in prompts.DOC_TYPES:
        raise HTTPException(400, f"알 수 없는 문서 유형: {req.doc_type}")
    if not req.text.strip():
        raise HTTPException(400, "양식 내용이 비어 있습니다.")

    try:
        path = store.save_format(
            resolved["id"], resolved["name"], req.doc_type, req.text)
    except store.StoreError as e:
        raise HTTPException(400, str(e))
    return {"saved": True, "path": path}


@app.get("/api/store")
def store_status():
    """문서 보관 기능 진단. 토큰·저장소·쓰기 권한을 실제로 확인한다."""
    return store.check()


class FeedbackRequest(BaseModel):
    text: str = Field(..., min_length=1)


@app.post("/api/feedback")
def feedback(req: FeedbackRequest):
    """익명 개선요청을 저장소에 보관한다. 사용자·기관 정보는 남기지 않는다."""
    if not store.enabled():
        raise HTTPException(400, "서버에 GITHUB_TOKEN 이 없어 개선요청을 저장할 수 없습니다.")
    try:
        store.save_feedback(req.text)
    except store.StoreError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/orgs")
def list_orgs():
    """기본 제공 기관(core/orgs.json) + 웹에서 추가돼 공유된 기관(datas/orgs.json)."""
    try:
        merged = load_orgs()
    except OrgError as e:
        raise HTTPException(500, str(e))

    ids = {o["id"] for o in merged}
    names = {o["name"] for o in merged}
    for raw in store.load_custom_orgs():
        try:
            o = normalize_org(raw)
        except OrgError:
            continue
        if o["id"] in ids or o["name"] in names:
            continue
        merged.append(o)
        ids.add(o["id"])
        names.add(o["name"])
    merged.sort(key=lambda o: o["name"])   # 이름 가나다순
    return {"orgs": merged}


class OrgCreate(BaseModel):
    name: str = Field(..., min_length=1)


@app.post("/api/orgs")
def create_org(req: OrgCreate):
    """웹에서 추가한 기관을 저장소에 공유 저장한다. 모든 사용자에게 보인다."""
    if not store.enabled():
        raise HTTPException(
            400, "서버에 GITHUB_TOKEN 이 없어 기관을 공유 저장할 수 없습니다.")
    try:
        org = normalize_org({"name": req.name})
    except OrgError as e:
        raise HTTPException(400, str(e))
    try:
        saved = store.add_custom_org(org)
    except store.StoreError as e:
        raise HTTPException(400, str(e))
    return {"org": saved}


# ============================================================
# 파일 → 텍스트
# ============================================================
@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "빈 파일입니다.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"파일이 너무 큽니다 ({len(data) / 1048576:.1f}MB). "
            f"{MAX_UPLOAD_BYTES // 1048576}MB 이하만 업로드할 수 있습니다.",
        )
    try:
        text = extract_from_bytes(file.filename or "", data)
    except ExtractError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"파일 해석 중 오류가 발생했습니다: {e}")

    if not text.strip():
        raise HTTPException(400, "문서에서 텍스트를 찾지 못했습니다.")
    return {"filename": file.filename, "text": text}


# ============================================================
# 작성 / 검토
# ============================================================
class HandoverFile(BaseModel):
    name: str
    text: str


class GenerateRequest(BaseModel):
    task: str = Field("write", description='"write" | "review" | "summary" | "handover"')
    doc_type: str = Field("", description="공문-외부 | 공문-내부 | 보고서 (요약은 불필요)")
    input: str = Field("", description="인수인계는 전임자 메모(선택)")
    # 인수인계 전용: 브라우저가 /api/extract 로 미리 추출한 자료들
    files: list[HandoverFile] = Field(default_factory=list)
    # 인수인계: 기관에 보관된 문서 중 관련 있는 것도 참고할지 (웹 화면은 항상 켬)
    use_org_docs: bool = True
    # 기본 기관은 id 문자열, 사용자가 추가한 기관은 객체를 통째로 보낸다
    org: object | None = Field(None, description="기관 id 또는 기관 객체")


def _sse(obj) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _org_candidates(org: dict) -> list:
    """참고 후보: 기관 보관 문서(공문·보고서 + 인수인계 때 올라온 자료). 실패하면 빈 목록."""
    try:
        return store.load_org_documents(
            org["id"], [*prompts.DOC_TYPES, store.HANDOVER_STORE_TYPE])
    except Exception:
        return []


def _name_docs(docs: list) -> list:
    """화면·프롬프트에 쓸 인용 이름("기관보관/유형/제목")을 붙인다."""
    taken = set()
    for d in docs:
        d["cite"] = prompts.org_doc_name(d, taken)
        taken.add(d["cite"])
    return docs


def _related_org_docs(org: dict, files: list) -> list:
    """기관 보관 문서 중 인수인계 자료와 관련 있는 것. 실패하면 빈 목록(작성은 계속)."""
    return _name_docs(related.rank([f["text"] for f in files], _org_candidates(org)))


def _write_references(org: dict, doc_type: str, request: str) -> list:
    """작성 참고 문서: 요청 내용과 관련 있는 기관 보관 문서(인수인계 자료 포함) 최대 3건.

    관련 문서가 없으면 예전처럼 같은 유형의 최근 문서를 양식·문체 참고로만 붙인다
    (이미 읽은 후보에서 고르므로 추가 조회 없음). 각 문서에 related 표시를 단다.
    """
    candidates = _org_candidates(org)
    picked = related.rank([request], candidates, min_score=related.MIN_SCORE_SHORT,
                          min_overlap=related.MIN_OVERLAP_SHORT)
    if picked:
        for d in picked:
            d["related"] = True
    else:
        picked = [dict(d, related=False) for d in candidates
                  if d["type"] == doc_type][:store.MAX_REFERENCES]
    for d in picked:
        d["text"] = d["text"][:store.MAX_REFERENCE_CHARS]
    return _name_docs(picked)


@app.post("/api/generate")
def generate(req: GenerateRequest):
    task = req.task if req.task in ("write", "review", "summary", "handover") else "write"
    handover_files = [f.model_dump() for f in req.files if f.text.strip()]
    if task == "handover":
        # 입력은 기관 보관 문서를 고른 뒤 스트림 안에서 조립한다
        user_input = ""
        if not handover_files:
            raise HTTPException(400, "인수인계 자료를 불러와 주세요.")
    else:
        user_input = req.input.strip()
        if not user_input:
            raise HTTPException(400, "입력 내용이 비어 있습니다.")
    try:
        org = resolve_org(req.org)
    except OrgError as e:
        raise HTTPException(400, str(e))
    try:
        system_prompt = prompts.get_system_prompt(task, req.doc_type, org)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 작성만 기관의 양식 정의와 관련 문서를 참고로 붙인다(관련 문서는 스트림 안에서 고른다).
    # 검토·요약은 유형을 미리 모르므로(AI 가 판정) 참고 자료를 붙이지 않는다.
    # 우선순위: 양식.md > 실제 문서 > 코드 기본 규칙. 조회 실패해도 진행한다.
    custom_format = None
    references = []
    if task == "write":
        custom_format = store.load_format(org["id"], req.doc_type)

    client = _client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input},
    ]
    # 인수인계서는 섹션이 많아 기본 출력 한도(4096)로는 중간에 잘린다
    max_tokens = 16384 if task == "handover" else None

    def event_stream():
        t0 = time.time()
        buf = []
        # 폴백은 스트림 생성 도중 발생하므로 큐에 모아뒀다가 다음 틈에 내보낸다
        fallbacks = []
        org_docs = []

        try:
            if task == "write" and store.enabled():
                # 관련 문서 고르기는 GitHub API 를 여러 번 부르므로 응답을 연 뒤에 한다
                yield _sse({"stage": "관련 문서 찾는 중..."})
                references.extend(_write_references(org, req.doc_type, user_input))
                yield _sse({"stage": "생성 중..."})
            if task == "write" and (custom_format or references):
                messages[0]["content"] = prompts.get_system_prompt(
                    task, req.doc_type, org, references, custom_format)
            if task == "handover":
                # 기관 보관 문서 조회는 GitHub API 를 여러 번 부르므로 스트림 안에서 한다
                # (응답이 먼저 열려 화면에 진행 상황이 보이도록)
                if req.use_org_docs and store.enabled():
                    yield _sse({"stage": "기관 보관 문서에서 관련 자료 찾는 중..."})
                    org_docs = _related_org_docs(org, handover_files)
                messages[1]["content"] = prompts.build_handover_input(
                    handover_files, req.input, org_docs)
                yield _sse({"stage": "인수인계서 작성 중..."
                            + (f" (기관 보관 문서 {len(org_docs)}건 참고)" if org_docs else "")})

            stream = client.stream(
                messages,
                max_output_tokens=max_tokens,
                on_fallback=lambda model, reason: fallbacks.append(
                    {"model": model, "reason": reason}),
            )
            for chunk in stream:
                while fallbacks:
                    yield _sse({"fallback": fallbacks.pop(0)})
                buf.append(chunk)
                yield _sse({"token": chunk})

            raw = "".join(buf)
            # 검토·요약 모두 첫 줄 "[유형] ○○" 로 AI 가 분류한다.
            # 그 유형으로 보관하고(save_type), 분류 줄은 화면 출력에서 제거한다.
            save_type = None
            if task in ("review", "summary"):
                save_type, raw = prompts.parse_summary_type(raw)

            final = prompts.strip_markdown(raw)
            # 공문 작성 모드에서만 "끝." 자동 보정 (검토·보고서는 대상 아님)
            if task == "write" and "공문" in req.doc_type:
                final = prompts.ensure_end_mark(final)

            # 검토·요약한 원문을 다음 작성 때 참고하도록, AI 가 분류한 유형에 보관.
            saved = None
            save_error = None
            if save_type and store.enabled():
                source = req.input.strip()
                try:
                    saved = store.save_document(
                        org["id"], org["name"], save_type,
                        prompts.extract_title(source, save_type),
                        source,
                    )
                except Exception as e:
                    # 보관 실패가 결과를 막지는 않되, 이유는 알려준다
                    save_error = str(e)

            # 인수인계에 올린 자료도 보관한다(커밋 한 번). 다음 인수인계 때 관련 자료 후보가 된다.
            saved_count = 1 if saved else 0
            if task == "handover" and store.enabled():
                yield _sse({"stage": "올린 자료 보관 중..."})
                try:
                    saved_count = len(store.save_documents_batch(
                        org["id"], org["name"], store.HANDOVER_STORE_TYPE, handover_files))
                except Exception as e:
                    save_error = str(e)

            yield _sse({
                "done": True,
                "text": final,
                "tokens": client.last_output_tokens,
                "model": client.last_model,
                "org": org["name"],
                "doc_type": save_type,
                "saved": bool(saved_count),
                "saved_count": saved_count,
                "save_error": save_error,
                "references": len(references),
                # 관련 문서로 고른 것만 이름을 알려준다(양식 참고용 최근 문서는 제외)
                "reference_names": [d["cite"] for d in references if d.get("related")],
                "custom_format": bool(custom_format),
                # 화면에서 출처 칩을 눌러 원문을 볼 수 있도록 모델에 넣은 그대로 돌려준다
                "org_docs": [{"name": d["cite"], "type": d["type"], "saved": d["saved"],
                              "text": d["text"][:prompts.HANDOVER_ORG_DOC_CHARS]}
                             for d in org_docs],
                "elapsed": round(time.time() - t0, 1),
            })
        except GeminiError as e:
            yield _sse({"error": str(e)})
        except Exception as e:
            yield _sse({"error": f"예기치 못한 오류: {e}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 프록시 버퍼링 방지 (SSE 핵심)
            "X-Accel-Buffering": "no",
        },
    )
