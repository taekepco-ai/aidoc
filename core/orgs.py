"""기관(조직) 정의 로딩 및 검증.

기본 기관 목록은 orgs.json 에 있고, 사용자가 웹에서 추가한 기관은
브라우저 localStorage 에 저장돼 요청마다 통째로 전달된다.
서버는 전달받은 기관 정보를 그대로 프롬프트에 넣으므로 반드시 정규화한다.
"""
import json
import re
from pathlib import Path

ORGS_PATH = Path(__file__).resolve().parent / "orgs.json"

MAX_NAME_LEN = 60
MAX_SELF_REF_LEN = 30
MAX_DEPARTMENTS = 20
MAX_DEPARTMENT_LEN = 40

# 프롬프트 구조를 깨뜨릴 수 있는 문자 제거 (제어문자, 대괄호 섹션 표기, 개행)
_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f\[\]]")


class OrgError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 한국어 오류 메시지."""


def _clean(value, max_len: int) -> str:
    if value is None:
        return ""
    text = _FORBIDDEN.sub(" ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def load_orgs() -> list:
    """orgs.json 의 기본 기관 목록."""
    try:
        raw = json.loads(ORGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        raise OrgError(f"orgs.json 형식 오류: {e}")
    return [normalize_org(o) for o in raw]


def get_org(org_id: str):
    for org in load_orgs():
        if org["id"] == org_id:
            return org
    return None


def normalize_org(raw) -> dict:
    """임의의 입력을 안전한 기관 dict 로 정규화한다."""
    if not isinstance(raw, dict):
        raise OrgError("기관 정보 형식이 올바르지 않습니다.")

    name = _clean(raw.get("name"), MAX_NAME_LEN)
    if not name:
        raise OrgError("기관 이름은 필수입니다.")

    short = _clean(raw.get("short"), MAX_NAME_LEN) or name
    alias = _clean(raw.get("alias"), MAX_NAME_LEN)
    self_ref = _clean(raw.get("self_ref"), MAX_SELF_REF_LEN) or "우리 기관"

    org_id = _clean(raw.get("id"), MAX_NAME_LEN)
    if not org_id:
        org_id = re.sub(r"\s+", "-", name)

    def _departments(key):
        items = raw.get(key) or []
        if isinstance(items, str):
            items = [p for p in re.split(r"[,\n]", items)]
        if not isinstance(items, (list, tuple)):
            return []
        out = []
        for item in items:
            cleaned = _clean(item, MAX_DEPARTMENT_LEN)
            if cleaned and cleaned not in out:
                out.append(cleaned)
            if len(out) >= MAX_DEPARTMENTS:
                break
        return out

    return {
        "id": org_id,
        "name": name,
        "short": short,
        "alias": alias,
        "self_ref": self_ref,
        "departments_letter": _departments("departments_letter"),
        "departments_report": _departments("departments_report"),
    }


def resolve_org(payload) -> dict:
    """요청에서 받은 기관 정보를 확정한다.

    - dict 이면 사용자 정의 기관으로 보고 정규화
    - 문자열이면 기본 목록에서 id 로 조회
    - 없으면 기본 목록의 첫 기관
    """
    if isinstance(payload, dict):
        return normalize_org(payload)

    defaults = load_orgs()
    if isinstance(payload, str) and payload.strip():
        for org in defaults:
            if org["id"] == payload.strip():
                return org
        raise OrgError(f"알 수 없는 기관입니다: {payload}")

    if not defaults:
        raise OrgError("등록된 기관이 없습니다. orgs.json 을 확인하세요.")
    return defaults[0]
