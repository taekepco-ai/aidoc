"""기관 보관 문서 중 인수인계 자료와 관련 있는 것만 고른다.

보관 문서는 그 기관의 여러 부서가 검토·요약에 올린 것이라, 전부 넣으면 전임자 업무와
무관한 내용이 인수인계서에 섞이고 입력이 커져 생성도 느려진다. 그래서 인수인계 자료와
낱말이 많이 겹치는 문서만 몇 건 고른다.

방식은 TF-IDF 코사인 유사도. 외부 라이브러리·임베딩 API 없이 표준 라이브러리로 돈다.
한국어는 조사가 붙어 낱말 모양이 바뀌므로("예산을", "예산은") 흔한 조사를 떼고 비교한다.
"""
import math
import re
from collections import Counter

# 관련 있다고 볼 최소 유사도와 최대 건수. 실제 보관 문서(datas/ 44건)로 맞춘 값:
# 같은 부서·업무 문서는 0.2 안팎, 무관한 다른 기관 문서도 상투어 때문에 0.15 까지 나온다.
MIN_SCORE = 0.15
MAX_PICK = 3
# 작성 탭은 입력이 "신규 직원 환영회 개최 안내" 처럼 한두 줄이라 점수가 낮게 나온다.
# 실제 보관 문서로 잰 값: 관련 문서 0.1~0.42, 무관한 문서 0.07 이하.
MIN_SCORE_SHORT = 0.09
# 짧은 입력은 낱말 하나만 겹쳐도 그 낱말 가중치 때문에 점수가 튄다("환영회 개최" ↔ "위원회 개최").
# 그래서 의미 있는 낱말이 이만큼 이상 겹칠 때만 관련 문서로 본다.
MIN_OVERLAP_SHORT = 2
# 올린 자료 중 하나와 이만큼 비슷하면 같은 문서(이미 보관된 사본)로 보고 뺀다
DUP_SCORE = 0.9

_WORD_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}|\d{4,}")
# 낱말 끝에서 뗄 조사·어미 (긴 것부터)
_JOSA = ("으로서", "으로써", "에서는", "에게서", "까지", "부터", "에서", "에게", "으로",
         "와의", "과의", "이며", "이고", "하여", "하고", "한다", "했다", "하는", "되는",
         "은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과", "도", "만", "및")
# 어느 문서에나 나오는 공문·보고서 상투어. 관련도를 부풀리므로 뺀다.
_STOP = {
    "관련", "추진", "계획", "보고", "결과", "실시", "시행", "검토", "협조", "요청", "안내",
    "다음", "아래", "같이", "위하여", "대한", "대하여", "따라", "통해", "있음", "없음", "예정",
    "내용", "사항", "기타", "붙임", "수신", "발신", "제목", "참조", "담당", "부서", "기관",
    "우리", "공사", "본부", "처장", "부장", "팀장", "사장", "합니다", "바랍니다", "있습니다",
    "주시기", "하시기", "드립니다", "년도", "금년", "올해", "현재", "향후", "필요",
    "개최", "운영", "현황", "방안", "대책", "신규", "추가", "진행", "완료", "변경",
}


def _tokens(text: str) -> list:
    out = []
    for w in _WORD_RE.findall(text or ""):
        if w[0] >= "가":                      # 한글 낱말만 조사를 뗀다
            for j in _JOSA:
                if len(w) > len(j) + 1 and w.endswith(j):
                    w = w[: -len(j)]
                    break
        if len(w) >= 2 and w not in _STOP:
            out.append(w)
    return out


def _vector(counts: Counter, idf: dict) -> dict:
    vec = {w: (1 + math.log(c)) * idf.get(w, 0.0) for w, c in counts.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {w: v / norm for w, v in vec.items()}


def _cos(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(w, 0.0) for w, v in a.items())


def rank(sources: list, docs: list, min_score: float = MIN_SCORE,
         limit: int = MAX_PICK, min_overlap: int = 1) -> list:
    """인수인계 자료(sources: 본문 목록)와 비슷한 docs 를 유사도 순으로 고른다.

    자료 전체를 한 덩어리로 보고 비교하되, 자료 중 하나와 거의 같은 문서는 뺀다.
    docs: [{"text": ..., ...}]  →  같은 dict 에 "score" 를 붙여 돌려준다.
    """
    sources = [s for s in (sources or []) if (s or "").strip()]
    if not docs or not sources:
        return []
    file_counts = [Counter(_tokens(s)) for s in sources]
    src_counts = sum(file_counts, Counter())
    doc_counts = [Counter(_tokens(d.get("text", ""))) for d in docs]
    if not src_counts:
        return []

    # 문서 빈도는 보관 문서 + 인수인계 자료 묶음을 한 문서로 쳐서 센다
    n = len(docs) + 1
    df = Counter()
    for c in (src_counts, *doc_counts):
        df.update(c.keys())
    idf = {w: math.log((n + 1) / (k + 1)) + 1 for w, k in df.items()}

    src_vec = _vector(src_counts, idf)
    file_vecs = [_vector(c, idf) for c in file_counts]
    scored = []
    for d, c in zip(docs, doc_counts):
        vec = _vector(c, idf)
        if any(_cos(vec, fv) >= DUP_SCORE for fv in file_vecs):
            continue
        score = _cos(vec, src_vec)
        if score >= min_score and len(c.keys() & src_counts.keys()) >= min_overlap:
            scored.append({**d, "score": round(score, 3)})
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:limit]
