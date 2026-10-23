"""평문 텍스트 → HWPX(한글) 문서 생성.

HWPX는 ZIP 기반 OWPML(KS X 6101) 패키지다. 생성 방식이 두 가지다.

1. 템플릿 방식 (권장·안정)
   core/hwpx_template.hwpx 가 있으면 그 안의 Contents/section0.xml 만 교체한다.
   한글에서 빈 문서를 .hwpx 로 저장해 그 경로에 두면 된다.
   header.xml 의 글꼴·스타일 ID를 한글이 실제로 쓰는 값 그대로 재사용하므로
   호환성 문제가 사실상 없다.

2. 직접 생성 (템플릿이 없을 때)
   core/hwpx_style.py 가 사내 보고서 서식(제목 상자·HY헤드라인 소제목·휴먼명조 본문·
   한글 표)으로 만든다. kanana-doc 에서 한글로 열어 검증한 구성을 그대로 쓴다.

주의: mimetype 은 반드시 ZIP 의 첫 엔트리이면서 무압축(STORED)이어야 한다.
      (ODF/EPUB 와 같은 규약)
"""
import io
import re
import zipfile
from pathlib import Path

from core.hwpx_style import build_styled

TEMPLATE_PATH = Path(__file__).resolve().parent / "hwpx_template.hwpx"

MIMETYPE = "application/hwp+zip"
SECTION_NAME = "Contents/section0.xml"
PRVTEXT_NAME = "Preview/PrvText.txt"

NS_HS = "http://www.hancom.co.kr/hwpml/2011/section"
NS_HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
NS_HC = "http://www.hancom.co.kr/hwpml/2011/core"

# A4 세로, 단위는 HWPUNIT(1/7200 inch)
PAGE_WIDTH = 59528     # 210mm
PAGE_HEIGHT = 84188    # 297mm
MARGIN_LR = 8504       # 30mm
MARGIN_TB = 5668       # 20mm
MARGIN_HF = 4252       # 15mm


class HwpxError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 한국어 오류 메시지."""


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _normalize(text: str) -> list:
    """줄 단위로 나눈다. 탭은 공백으로 바꾼다(HWPX 에서 별도 요소라 단순화)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return text.split("\n")


# ============================================================
# section0.xml
# ============================================================
_SEC_PR = f"""<hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="1134" tabStop="8000" tabStopVal="4000" tabStopUnit="HWPUNIT" outlineShapeIDRef="1" memoShapeIDRef="0" textVerticalWidthHead="0" masterPageCnt="0"><hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0" strtnumSet="0"/><hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/><hp:visibility hideFirstHeader="0" hideFirstFooter="0" hideFirstMasterPage="0" border="SHOW_ALL" fill="SHOW_ALL" hideFirstPageNum="0" hideFirstEmptyLine="0" showLineNumber="0"/><hp:lineNumberShape restartType="0" countBy="0" distance="0" startNumber="0"/><hp:pagePr landscape="WIDELY" width="{PAGE_WIDTH}" height="{PAGE_HEIGHT}" gutterType="LEFT_ONLY"><hp:margin header="{MARGIN_HF}" footer="{MARGIN_HF}" gutter="0" left="{MARGIN_LR}" right="{MARGIN_LR}" top="{MARGIN_TB}" bottom="{MARGIN_TB}"/></hp:pagePr><hp:footNotePr><hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/><hp:noteLine length="-1" type="SOLID" width="0.12 mm" color="#000000"/><hp:noteSpacing betweenNotes="850" belowLine="567" aboveLine="850"/><hp:numbering type="CONTINUOUS" newNum="1"/><hp:placement place="EACH_COLUMN" beneathText="0"/></hp:footNotePr><hp:endNotePr><hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/><hp:noteLine length="14692344" type="SOLID" width="0.12 mm" color="#000000"/><hp:noteSpacing betweenNotes="0" belowLine="567" aboveLine="850"/><hp:numbering type="CONTINUOUS" newNum="1"/><hp:placement place="END_OF_DOCUMENT" beneathText="0"/></hp:endNotePr><hp:pageBorderFill type="BOTH" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER"><hp:offset left="1417" right="1417" top="1417" bottom="1417"/></hp:pageBorderFill></hp:secPr>"""


def build_section_xml(text: str) -> str:
    lines = _normalize(text)
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<hs:sec xmlns:hs="{NS_HS}" xmlns:hp="{NS_HP}" xmlns:hc="{NS_HC}">',
    ]
    for i, line in enumerate(lines):
        # 첫 문단의 run 안에 구역 설정(secPr)이 들어가야 한다
        sec_pr = _SEC_PR if i == 0 else ""
        parts.append(
            f'<hp:p id="{i}" paraPrIDRef="0" styleIDRef="0" pageBreak="0" '
            f'columnBreak="0" merged="0">'
            f'<hp:run charPrIDRef="0">{sec_pr}<hp:t>{_esc(line)}</hp:t></hp:run>'
            f'</hp:p>'
        )
    parts.append("</hs:sec>")
    return "".join(parts)


# ============================================================
# 원본 서식 물려받기
# ============================================================
_SEC_OPEN_RE = re.compile(r"<(\w+):sec\b[^>]*>")
_P_OPEN_RE = re.compile(r"<(\w+):p\b([^>]*?)/?>")
_RUN_OPEN_RE = re.compile(r"<(\w+):run\b([^>]*?)/?>")
_SECPR_PAIR_RE = re.compile(r"<(\w+):secPr\b.*?</\1:secPr>", re.DOTALL)
_SECPR_SELF_RE = re.compile(r"<(\w+):secPr\b[^>]*/>")
_ID_ATTR_RE = re.compile(r'\bid="[^"]*"')


def _set_id(attrs: str, index: int) -> str:
    if _ID_ATTR_RE.search(attrs):
        return _ID_ATTR_RE.sub(f'id="{index}"', attrs, count=1)
    return f' id="{index}"' + attrs


def build_section_from_original(text: str, original_xml: str) -> str:
    """원본 section0.xml 의 서식을 물려받아 본문만 교체한다.

    가져오는 것
      - 네임스페이스 접두사와 hs:sec 여는 태그 (2011/2021 스키마 차이 흡수)
      - hp:secPr 블록 통째로 (용지 크기·여백·머리말 설정)
      - 첫 문단·run 의 paraPrIDRef / styleIDRef / charPrIDRef (글꼴·문단 모양)

    구조를 못 읽으면 None 을 돌려주고 호출부가 기본 생성으로 넘어간다.
    """
    sec_open = _SEC_OPEN_RE.search(original_xml)
    p_open = _P_OPEN_RE.search(original_xml)
    if not sec_open or not p_open:
        return None

    sec_prefix = sec_open.group(1)
    p_prefix, p_attrs = p_open.group(1), p_open.group(2)

    run_open = _RUN_OPEN_RE.search(original_xml)
    run_attrs = run_open.group(2) if run_open else ' charPrIDRef="0"'
    run_prefix = run_open.group(1) if run_open else p_prefix

    secpr = ""
    m = _SECPR_PAIR_RE.search(original_xml) or _SECPR_SELF_RE.search(original_xml)
    if m:
        secpr = m.group(0)

    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        sec_open.group(0),
    ]
    for i, line in enumerate(_normalize(text)):
        # 구역 설정은 첫 문단의 run 안에만 들어간다
        head = secpr if i == 0 else ""
        parts.append(
            f'<{p_prefix}:p{_set_id(p_attrs, i)}>'
            f'<{run_prefix}:run{run_attrs}>{head}'
            f'<{run_prefix}:t>{_esc(line)}</{run_prefix}:t>'
            f'</{run_prefix}:run></{p_prefix}:p>'
        )
    parts.append(f"</{sec_prefix}:sec>")
    return "".join(parts)


# ============================================================
# 템플릿 방식
# ============================================================
def _build_from_template(text: str, template) -> bytes:
    """template: 파일 경로 또는 hwpx 바이트."""
    out = io.BytesIO()
    source = io.BytesIO(template) if isinstance(template, (bytes, bytearray)) \
        else str(template)

    try:
        src_zip = zipfile.ZipFile(source, "r")
    except zipfile.BadZipFile:
        raise HwpxError(
            "업로드한 파일을 서식 원본으로 쓸 수 없습니다. "
            "한글에서 저장한 정상적인 .hwpx 인지 확인하세요."
        )

    with src_zip as src:
        names = src.namelist()
        if SECTION_NAME not in names:
            raise HwpxError(
                f"서식 원본에 {SECTION_NAME} 이 없습니다. "
                "한글에서 저장한 정상적인 .hwpx 인지 확인하세요."
            )

        try:
            original = src.read(SECTION_NAME).decode("utf-8")
        except Exception:
            original = ""

        section = (build_section_from_original(text, original)
                   or build_section_xml(text)).encode("utf-8")

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            # mimetype 을 가장 먼저, 무압축으로
            if "mimetype" in names:
                dst.writestr(
                    zipfile.ZipInfo("mimetype"),
                    src.read("mimetype"),
                    compress_type=zipfile.ZIP_STORED,
                )
            for name in names:
                if name == "mimetype":
                    continue
                if name == SECTION_NAME:
                    dst.writestr(name, section)
                elif name == PRVTEXT_NAME:
                    dst.writestr(name, text.encode("utf-8"))
                elif re.match(r"Contents/section[1-9]\d*\.xml$", name):
                    # 템플릿에 2번째 이후 구역이 있으면 버린다 (본문은 한 구역)
                    continue
                else:
                    dst.writestr(name, src.read(name))
    return out.getvalue()


# ============================================================
# 진입점
# ============================================================
def build_hwpx(text: str, title: str = "문서", template=None) -> bytes:
    """평문 텍스트를 HWPX 바이트로 만든다.

    template 우선순위
      1. 인자로 받은 hwpx 바이트 또는 경로 (보통 사용자가 업로드한 원본)
      2. core/hwpx_template.hwpx
      3. 없으면 사내 보고서 서식으로 직접 생성 (core/hwpx_style.py)
         제목 상자·소제목·본문 글꼴과, "| 칸 | 칸 |" 줄을 실제 한글 표로 바꾸는 것은
         이 경우에만 적용된다. 서식 원본을 쓰면 원본 글꼴을 물려받는 대신 표는 글자로 남는다.
    """
    if not text or not text.strip():
        raise HwpxError("저장할 내용이 없습니다.")

    if isinstance(template, (bytes, bytearray)) and template:
        return _build_from_template(text, template)

    if template:
        path = Path(template)
        if path.exists():
            return _build_from_template(text, path)

    if TEMPLATE_PATH.exists():
        return _build_from_template(text, TEMPLATE_PATH)

    return build_styled(text, title or "문서")


def using_template() -> bool:
    return TEMPLATE_PATH.exists()
