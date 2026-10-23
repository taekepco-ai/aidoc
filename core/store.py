"""검토 완료 문서를 GitHub 저장소에 보관하고, 작성 시 참고 자료로 불러온다.

저장 경로
    datas/<기관 id>/<문서 유형>/<YYYYMMDD-HHMMSS>-<제목>.txt

서버리스에는 쓸 수 있는 파일 시스템이 없으므로 GitHub Contents API 로 직접
커밋한다. 커밋 메시지에 SKIP_MARKER 를 넣어 Vercel 재배포를 막는다
(vercel.json 의 ignoreCommand 와 짝을 이룬다).

환경 변수
    GITHUB_TOKEN   저장소 contents 쓰기 권한 (없으면 기능 전체가 꺼진다)
    GITHUB_REPO    "owner/repo"  (필수, 기본값 없음)
    GITHUB_BRANCH  기본값 main
"""
import base64
import json
import os
import re
from datetime import datetime
from urllib.parse import quote

API = "https://api.github.com"

REPO = os.environ.get("GITHUB_REPO", "").strip()
BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip()
ROOT = "datas"

# 이 표식이 있는 커밋은 Vercel 이 빌드를 건너뛴다
SKIP_MARKER = "[skip deploy]"

# 작성 시 참고할 문서 수와 문서당 최대 길이 (프롬프트 비대화 방지, api/index.py 에서 사용)
MAX_REFERENCES = 3
MAX_REFERENCE_CHARS = 2500

# 기관·유형별 양식 정의 파일. 저장소에서 바로 읽으므로 고치면 재배포 없이 반영된다.
FORMAT_FILENAME = "양식.md"
MAX_FORMAT_CHARS = 4000

# 웹에서 추가한 공유 기관 목록. core/orgs.json(기본 제공) 과 별개로 저장소에 둔다.
CUSTOM_ORGS_PATH = f"{ROOT}/orgs.json"

# 익명 개선요청 보관 폴더 (datas 와 분리, 사람이 GitHub 에서 읽는 용도)
FEEDBACK_DIR = "feedback"
MAX_FEEDBACK_CHARS = 3000

# 저장 대상 문서 길이 제한
MIN_SAVE_CHARS = 50
MAX_SAVE_CHARS = 20000

_TIMEOUT = 15


class StoreError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 한국어 오류 메시지."""


def token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def enabled() -> bool:
    return bool(token() and REPO)


def check() -> dict:
    """토큰·저장소·권한을 실제로 확인한다. 진단용."""
    info = {
        "enabled": enabled(),
        "repo": REPO,
        "branch": BRANCH,
        "path": f"{ROOT}/<기관 id>/<문서 유형>/",
        "ok": False,
        "error": None,
    }
    if not token():
        info["error"] = ("GITHUB_TOKEN 이 설정되지 않았습니다. "
                         "Vercel 환경 변수에 등록한 뒤 재배포해야 적용됩니다.")
        return info
    if not REPO:
        info["error"] = "GITHUB_REPO 가 비어 있습니다."
        return info

    import requests
    try:
        r = requests.get(f"{API}/repos/{REPO}", headers=_headers(), timeout=_TIMEOUT)
    except Exception as e:
        info["error"] = f"GitHub 연결 실패: {e}"
        return info

    if not r.ok:
        info["error"] = _error(r)
        return info

    body = r.json() or {}
    info["private"] = body.get("private")
    info["default_branch"] = body.get("default_branch")
    if not (body.get("permissions") or {}).get("push"):
        info["error"] = (
            "토큰이 저장소를 읽을 수는 있으나 쓰기 권한이 없습니다. "
            "Fine-grained token 이라면 이 저장소를 선택하고 "
            "Repository permissions → Contents 를 Read and write 로 설정하세요."
        )
        return info

    info["ok"] = True
    return info


def _headers():
    return {
        "Authorization": f"Bearer {token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _safe(part: str, limit: int = 60) -> str:
    """경로에 쓸 수 있게 정리한다. 한글은 그대로 두고 위험 문자만 제거."""
    part = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(part))
    part = re.sub(r"\s+", " ", part).strip(" .")
    return part[:limit] or "무제"


def _dir_path(org_id: str, doc_type: str) -> str:
    return f"{ROOT}/{_safe(org_id)}/{_safe(doc_type)}"


def _url(path: str) -> str:
    return f"{API}/repos/{REPO}/contents/{quote(path)}"


# ============================================================
# 저장
# ============================================================
def _dir_names(org_id: str, doc_type: str) -> list:
    """폴더 안 파일명 목록(내용은 안 가져옴). 한 번의 API 호출."""
    import requests
    r = requests.get(_url(_dir_path(org_id, doc_type)), headers=_headers(),
                     params={"ref": BRANCH}, timeout=_TIMEOUT)
    if not r.ok:
        return []
    items = r.json()
    if not isinstance(items, list):
        return []
    return [it.get("name", "") for it in items if it.get("type") == "file"]


# AI 챗봇 답변으로 보이는 원문(참고 자료로 부적합). 저장하지 않는다.
_AI_HEAD_RE = re.compile(r"^\s*(네|예|알겠습니다|안녕하세요|물론(이죠|입니다)?)[,.\s]")
_AI_PHRASE_RE = re.compile(r"(정리해\s*드리|작성해\s*드리|도와\s*드리|아래(와)?\s*같이)"
                           r".{0,20}(습니다|겠습니다|드릴게요)")


def looks_like_ai_output(body: str) -> bool:
    head = body[:300]
    if _AI_HEAD_RE.match(head):
        return True
    if _AI_PHRASE_RE.search(head):
        return True
    # 마크다운 강조 + 구분선이 함께 있으면 AI 서식으로 본다
    if "**" in body and re.search(r"\n-{3,}\n", body):
        return True
    return False


def save_document(org_id: str, org_name: str, doc_type: str,
                  title: str, text: str) -> str | None:
    """문서를 저장하고 경로를 돌려준다. 저장하지 않았으면 None.

    저장 품질 게이트: 너무 짧거나, AI 출력물처럼 보이거나, 이미 같은 내용이
    있으면(중복) 저장하지 않는다.
    """
    if not enabled():
        return None

    body = (text or "").strip()
    if len(body) < MIN_SAVE_CHARS:
        return None
    if looks_like_ai_output(body):
        return None
    if len(body) > MAX_SAVE_CHARS:
        body = body[:MAX_SAVE_CHARS]

    import hashlib
    import requests

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]
    # 같은 내용(해시)이 이미 있으면 중복이므로 건너뛴다
    try:
        if any(digest in name for name in _dir_names(org_id, doc_type)):
            return None
    except Exception:
        pass

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = f"{_dir_path(org_id, doc_type)}/{stamp}-{_safe(title, 40)}-{digest}.txt"

    header = f"기관: {org_name}\n유형: {doc_type}\n저장: {stamp}\n{'-' * 40}\n"
    content = base64.b64encode((header + body).encode("utf-8")).decode("ascii")

    r = requests.put(
        _url(path),
        headers=_headers(),
        json={
            "message": f"문서 보관: {org_name} / {doc_type} {SKIP_MARKER}",
            "content": content,
            "branch": BRANCH,
        },
        timeout=_TIMEOUT,
    )
    if r.status_code in (200, 201):
        return path
    raise StoreError(_error(r))


# 인수인계에 올린 자료의 보관 폴더(유형 자리). 공문·보고서가 아닌 엑셀·메일·메모가 섞여 있어
# 작성 탭의 양식 참고(유형별 폴더)와 분리하고, 다음 인수인계 때 관련 자료 후보로만 쓴다.
HANDOVER_STORE_TYPE = "인수인계자료"


def save_documents_batch(org_id: str, org_name: str, doc_type: str,
                         docs: list) -> list:
    """여러 문서를 커밋 한 번으로 보관한다. 보관한 경로 목록을 돌려준다.

    docs: [{"name": 원본 파일명, "text": 본문}]
    save_document 와 같은 품질 게이트(짧음·AI 출력물·중복)를 거친다.
    파일마다 Contents API 로 커밋하면 폴더째 올린 자료가 수십 번 커밋되고 느리므로,
    Git Data API 로 트리 하나에 모아 한 번에 커밋한다.
    """
    if not enabled():
        return []

    import hashlib
    import requests

    try:
        existing = _dir_names(org_id, doc_type)
    except Exception:
        existing = []

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    entries, seen = [], set()
    for d in docs:
        body = (d.get("text") or "").strip()
        if len(body) < MIN_SAVE_CHARS or looks_like_ai_output(body):
            continue
        body = body[:MAX_SAVE_CHARS]
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]
        # 이미 보관된 내용이거나 이번 묶음 안에서 겹치면 건너뛴다
        if digest in seen or any(digest in name for name in existing):
            continue
        seen.add(digest)
        source = d.get("name") or "자료"
        title = re.sub(r"\.[^./\\]+$", "", source.replace("\\", "/").split("/")[-1])
        path = f"{_dir_path(org_id, doc_type)}/{stamp}-{_safe(title, 40)}-{digest}.txt"
        header = (f"기관: {org_name}\n유형: {doc_type}\n원본: {source}\n"
                  f"저장: {stamp}\n{'-' * 40}\n")
        entries.append({"path": path, "mode": "100644", "type": "blob",
                        "content": header + body})
    if not entries:
        return []

    repo_api = f"{API}/repos/{REPO}/git"
    message = f"인수인계 자료 보관: {org_name} {len(entries)}건 {SKIP_MARKER}"
    # 그 사이 다른 커밋(검토 보관 등)이 끼어들면 ref 갱신이 거절되므로 최신 기준으로 다시 시도
    for _ in range(3):
        r = requests.get(f"{repo_api}/ref/heads/{BRANCH}", headers=_headers(), timeout=_TIMEOUT)
        if not r.ok:
            raise StoreError(_error(r))
        head = r.json()["object"]["sha"]
        r = requests.get(f"{repo_api}/commits/{head}", headers=_headers(), timeout=_TIMEOUT)
        if not r.ok:
            raise StoreError(_error(r))
        base_tree = r.json()["tree"]["sha"]

        r = requests.post(f"{repo_api}/trees", headers=_headers(), timeout=60,
                          json={"base_tree": base_tree, "tree": entries})
        if not r.ok:
            raise StoreError(_error(r))
        r = requests.post(f"{repo_api}/commits", headers=_headers(), timeout=_TIMEOUT,
                          json={"message": message, "tree": r.json()["sha"], "parents": [head]})
        if not r.ok:
            raise StoreError(_error(r))
        r = requests.patch(f"{repo_api}/refs/heads/{BRANCH}", headers=_headers(),
                           timeout=_TIMEOUT, json={"sha": r.json()["sha"]})
        if r.ok:
            return [e["path"] for e in entries]
        if r.status_code != 422:          # 422 = fast-forward 아님(그 사이 새 커밋)
            raise StoreError(_error(r))
    raise StoreError("다른 저장과 겹쳐 인수인계 자료를 보관하지 못했습니다. 잠시 후 다시 시도하세요.")


# ============================================================
# 조회
# ============================================================
def list_documents(org_id: str, doc_type: str, limit: int = MAX_REFERENCES) -> list:
    """최근 문서 파일 목록 (최신순). 폴더가 없으면 빈 목록."""
    if not enabled():
        return []

    import requests

    r = requests.get(
        _url(_dir_path(org_id, doc_type)),
        headers=_headers(),
        params={"ref": BRANCH},
        timeout=_TIMEOUT,
    )
    if r.status_code == 404:
        return []
    if not r.ok:
        raise StoreError(_error(r))

    items = r.json()
    if not isinstance(items, list):
        return []
    files = [
        it for it in items
        if it.get("type") == "file" and it.get("name", "").endswith(".txt")
    ]
    # 파일명이 타임스탬프로 시작하므로 역순 정렬이 곧 최신순
    files.sort(key=lambda it: it.get("name", ""), reverse=True)
    return files[:limit]


def load_custom_orgs() -> list:
    """웹에서 추가돼 저장소에 공유된 기관 목록. 없으면 빈 목록."""
    if not enabled():
        return []
    import requests
    try:
        r = requests.get(_url(CUSTOM_ORGS_PATH), headers=_headers(),
                         params={"ref": BRANCH}, timeout=_TIMEOUT)
        if not r.ok:          # 404 = 아직 추가된 기관 없음 (정상)
            return []
        raw = base64.b64decode(r.json().get("content", "")).decode("utf-8")
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def add_custom_org(org: dict) -> dict:
    """공유 기관 목록에 추가한다. 같은 이름이 이미 있으면 그대로 둔다."""
    if not enabled():
        raise StoreError("GITHUB_TOKEN 이 설정되지 않았습니다.")

    import requests

    sha, items = None, []
    r = requests.get(_url(CUSTOM_ORGS_PATH), headers=_headers(),
                     params={"ref": BRANCH}, timeout=_TIMEOUT)
    if r.ok:
        sha = r.json().get("sha")
        try:
            items = json.loads(
                base64.b64decode(r.json().get("content", "")).decode("utf-8"))
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []

    if any(isinstance(o, dict) and o.get("name") == org["name"] for o in items):
        return org   # 이미 있음

    items.append(org)
    payload = {
        "message": f"기관 추가: {org['name']} {SKIP_MARKER}",
        "content": base64.b64encode(
            json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha

    put = requests.put(_url(CUSTOM_ORGS_PATH), headers=_headers(),
                       json=payload, timeout=_TIMEOUT)
    if put.status_code in (200, 201):
        return org
    raise StoreError(_error(put))


def save_feedback(text: str) -> str:
    """익명 개선요청을 feedback/<시각>.txt 로 저장한다. 사용자 정보는 남기지 않는다."""
    if not enabled():
        raise StoreError("GITHUB_TOKEN 이 설정되지 않았습니다.")

    body = (text or "").strip()
    if not body:
        raise StoreError("내용이 비어 있습니다.")
    body = body[:MAX_FEEDBACK_CHARS]

    import requests

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = f"{FEEDBACK_DIR}/{stamp}.txt"
    content = base64.b64encode((f"제출: {stamp}\n{'-' * 40}\n{body}\n").encode("utf-8")).decode("ascii")

    r = requests.put(
        _url(path),
        headers=_headers(),
        json={
            "message": f"개선요청 접수 {SKIP_MARKER}",   # 내용은 커밋 메시지에 넣지 않음(익명)
            "content": content,
            "branch": BRANCH,
        },
        timeout=_TIMEOUT,
    )
    if r.status_code in (200, 201):
        return path
    raise StoreError(_error(r))


def load_format(org_id: str, doc_type: str) -> str | None:
    """기관·유형별 양식 정의(양식.md). 없으면 None.

    저장소에서 매 요청마다 읽으므로 GitHub 에서 고치면 재배포 없이 바로 반영된다.
    """
    if not enabled():
        return None

    import requests

    path = f"{_dir_path(org_id, doc_type)}/{FORMAT_FILENAME}"
    try:
        r = requests.get(_url(path), headers=_headers(),
                         params={"ref": BRANCH}, timeout=_TIMEOUT)
        if not r.ok:          # 404 = 아직 정의 안 함 (정상)
            return None
        raw = base64.b64decode(r.json().get("content", "")).decode("utf-8")
    except Exception:
        return None

    raw = raw.strip()
    return raw[:MAX_FORMAT_CHARS] if raw else None


def save_format(org_id: str, org_name: str, doc_type: str, text: str) -> str:
    """양식 정의를 만들거나 덮어쓴다."""
    if not enabled():
        raise StoreError("GITHUB_TOKEN 이 설정되지 않았습니다.")

    import requests

    path = f"{_dir_path(org_id, doc_type)}/{FORMAT_FILENAME}"

    # 이미 있으면 덮어쓰기 위해 sha 가 필요하다
    sha = None
    try:
        prev = requests.get(_url(path), headers=_headers(),
                            params={"ref": BRANCH}, timeout=_TIMEOUT)
        if prev.ok:
            sha = prev.json().get("sha")
    except Exception:
        pass

    payload = {
        "message": f"양식 수정: {org_name} / {doc_type} {SKIP_MARKER}",
        "content": base64.b64encode(text.strip().encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(_url(path), headers=_headers(), json=payload, timeout=_TIMEOUT)
    if r.status_code in (200, 201):
        return path
    raise StoreError(_error(r))


def _fetch_body(path: str) -> str:
    """보관 문서 본문(저장 때 붙인 머리말 제외). 실패하면 빈 문자열."""
    import requests
    try:
        r = requests.get(_url(path), headers=_headers(),
                         params={"ref": BRANCH}, timeout=_TIMEOUT)
        if not r.ok:
            return ""
        raw = base64.b64decode(r.json().get("content", "")).decode("utf-8")
    except Exception:
        return ""
    return raw.split("-" * 40, 1)[-1].strip()


# 인수인계 참고용으로 훑어볼 기관 보관 문서 수(최신순). 본문을 모두 읽어 관련도를
# 매기므로 GitHub API 호출 수가 이만큼 든다. 병렬로 읽어 1~2초 안쪽.
MAX_ORG_CANDIDATES = 30


def load_org_documents(org_id: str, doc_types,
                       limit: int = MAX_ORG_CANDIDATES) -> list:
    """기관에 보관된 문서를 유형 구분 없이 최신순으로 읽는다. 실패해도 예외 없음.

    [{"type", "name", "saved", "text"}] — name 은 저장 파일명(타임스탬프 포함).
    """
    if not enabled():
        return []
    from concurrent.futures import ThreadPoolExecutor

    items = []
    for doc_type in doc_types:
        try:
            items += [(doc_type, it) for it in list_documents(org_id, doc_type, limit)]
        except Exception:
            continue
    # 파일명이 타임스탬프로 시작하므로 이름 역순이 곧 최신순
    items.sort(key=lambda x: x[1].get("name", ""), reverse=True)
    items = items[:limit]

    with ThreadPoolExecutor(max_workers=8) as pool:
        bodies = list(pool.map(lambda x: _fetch_body(x[1]["path"]), items))

    out = []
    for (doc_type, it), body in zip(items, bodies):
        if not body:
            continue
        name = it.get("name", "")
        m = re.match(r"(\d{4})(\d{2})(\d{2})-", name)
        out.append({
            "type": doc_type,
            "name": name,
            "saved": f"{m.group(1)}. {int(m.group(2))}. {int(m.group(3))}." if m else "",
            "text": body,
        })
    return out


def _error(resp) -> str:
    try:
        msg = resp.json().get("message", "")
    except Exception:
        msg = (resp.text or "")[:200]
    if resp.status_code in (401, 403):
        return f"GitHub 인증 실패({resp.status_code}). GITHUB_TOKEN 권한을 확인하세요. {msg}"
    if resp.status_code == 404:
        return f"저장소를 찾을 수 없습니다: {REPO} ({msg})"
    if resp.status_code == 409:
        return f"저장소 상태 충돌(409). 잠시 후 재시도하세요. {msg}"
    return f"GitHub API 오류 {resp.status_code}: {msg}"
