"""업로드 문서 → 평문 텍스트 추출.

지원: .hwpx / .hwp / .docx / .pdf / .txt / .md
     인수인계 자료는 추가로 .xlsx / .csv / .eml
웹(api/index.py)과 데스크톱(desktop/gen.py)이 공유한다.
"""
import io
import re
from pathlib import Path

SUPPORTED_EXTS = (".hwpx", ".hwp", ".docx", ".pdf", ".txt", ".md")
# 인수인계는 업무 폴더를 통째로 받으므로 엑셀·메일까지 읽는다.
# 검토·요약은 공문·보고서만 대상이라 SUPPORTED_EXTS 를 그대로 쓴다.
HANDOVER_EXTS = SUPPORTED_EXTS + (".xlsx", ".csv", ".eml")

_HP_NS = "http://www.hancom.co.kr/hwpml/2012/paragraph"


class ExtractError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 한국어 오류 메시지."""


# 참고: 한글에서 자간을 벌려 놓으면 추출 시 "명 예 퇴 직" 처럼 글자마다 공백이 낀다.
# 공백을 기계적으로 없애면 문장 전체가 벌어진 경우 단어 경계까지 사라지므로
# ("...위원회를 다음과 같이" → "...위원회를다음과같이") 여기서는 손대지 않는다.
# 대신 core/prompts.py 의 REVIEW_COMMON 이 자간·띄어쓰기를 지적 대상에서 제외한다.
def _collapse(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ============================================================
# 형식별 추출
# ============================================================
def _local(tag: str) -> str:
    """네임스페이스를 뗀 로컬 태그명. '{...}p' -> 'p'"""
    return tag.rsplit("}", 1)[-1]


def _hwpx_flush(lines: list, buf: list) -> None:
    if buf:
        lines.append("".join(buf))
        buf.clear()


def _hwpx_cell_text(el) -> str:
    """요소 아래 모든 <t> 텍스트를 한 덩어리로 모은다(표 셀·중첩 표용)."""
    parts = []
    for t in el.iter():
        if _local(t.tag) == "t":
            txt = "".join(t.itertext())
            if txt:
                parts.append(txt)
    return " ".join(parts).strip()


def _hwpx_walk(el, lines: list, buf: list) -> None:
    """문서 순서로 훑되 표는 행/열 구조를 살려 추출한다.

    - 문단(p): 줄 경계
    - 텍스트(t): itertext() 로 인라인 요소 뒤 텍스트까지 수집
    - 표(tbl): 행(tr)마다 한 줄, 셀(tc)은 " | " 로 구분 → 표가 뒤섞이지 않는다
    """
    lt = _local(el.tag)

    if lt == "tbl":
        _hwpx_flush(lines, buf)
        got = False
        for tr in el:
            if _local(tr.tag) != "tr":
                continue
            cells = [_hwpx_cell_text(tc) for tc in tr if _local(tc.tag) == "tc"]
            row = " | ".join(cells)
            if row.strip(" |"):
                lines.append(row)
                got = True
        if not got:
            # tr/tc 구조를 못 읽으면 표 전체 텍스트라도 살린다
            txt = _hwpx_cell_text(el)
            if txt:
                lines.append(txt)
        return

    if lt == "t":
        txt = "".join(el.itertext())
        if txt:
            buf.append(txt)
        return

    if lt == "p":
        _hwpx_flush(lines, buf)

    for child in el:
        _hwpx_walk(child, lines, buf)


def _extract_hwpx(src) -> str:
    """HWPX = zip + XML. 표준 라이브러리만 사용.

    한글 버전마다 네임스페이스(2010/2011/2012/2021)가 달라서, 고정 대신
    로컬 태그명(p / t)으로 매칭한다. 표는 행/열 구조를 살려 추출한다.
    """
    import zipfile
    import xml.etree.ElementTree as ET

    lines = []
    with zipfile.ZipFile(src, "r") as zf:
        sections = sorted(
            n for n in zf.namelist()
            if n.startswith("Contents/section") and n.endswith(".xml")
        )
        if not sections:
            raise ExtractError("HWPX 안에서 본문(Contents/section*.xml)을 찾지 못했습니다.")
        for name in sections:
            with zf.open(name) as f:
                try:
                    root = ET.parse(f).getroot()
                except ET.ParseError:
                    continue
                buf = []
                _hwpx_walk(root, lines, buf)
                _hwpx_flush(lines, buf)
    return _collapse("\n".join(lines))


def _extract_hwp(data: bytes) -> str:
    """구형 바이너리 HWP 5.0. olefile 로 본문 스트림을 직접 파싱한다.

    HWP 5.0 = OLE 복합문서. FileHeader 로 압축·암호 여부를 읽고,
    BodyText/SectionN 스트림(압축 시 raw-deflate)에서 문단 텍스트 레코드만 뽑는다.
    표·그림·서식은 버리고 텍스트만 얻는다(검토용으로 충분).
    """
    import struct
    import zlib
    try:
        import olefile
    except ImportError:
        raise ExtractError(
            ".hwp 처리 모듈(olefile)이 없습니다. .hwpx 로 저장 후 올려 주세요.")

    buf = io.BytesIO(data)
    if not olefile.isOleFile(buf):
        raise ExtractError(
            "올바른 HWP 5.0 파일이 아닙니다. .hwpx 로 저장 후 올려 주세요.")

    ole = olefile.OleFileIO(buf)
    try:
        if not ole.exists("FileHeader"):
            raise ExtractError(
                "HWP 파일 구조를 인식하지 못했습니다. .hwpx 로 저장 후 올려 주세요.")
        header = ole.openstream("FileHeader").read()
        if header[:17] != b"HWP Document File":
            raise ExtractError(
                "HWP 5.0 형식이 아닙니다. .hwpx 로 저장 후 올려 주세요.")
        props = header[36] if len(header) > 36 else 0
        compressed = bool(props & 0x01)
        if props & 0x02:
            raise ExtractError("암호가 걸린 HWP 는 열 수 없습니다. 암호를 풀고 다시 올려 주세요.")

        sections = [
            e for e in ole.listdir()
            if len(e) == 2 and e[0] == "BodyText" and e[1].startswith("Section")
        ]
        sections.sort(key=lambda e: int(re.sub(r"\D", "", e[1]) or 0))

        parts = []
        for entry in sections:
            raw = ole.openstream(entry).read()
            if compressed:
                try:
                    raw = zlib.decompress(raw, -15)
                except Exception:
                    continue
            parts.append(_hwp_section_text(raw, struct))
    finally:
        ole.close()

    text = _collapse("\n".join(parts))
    if not text.strip():
        raise ExtractError(
            "HWP 에서 텍스트를 찾지 못했습니다. .hwpx 로 저장 후 올려 주세요.")
    return text


def _hwp_section_text(data: bytes, struct) -> str:
    """섹션 레코드 스트림에서 문단 텍스트(HWPTAG_PARA_TEXT=67)만 모은다."""
    out, i, n = [], 0, len(data)
    while i + 4 <= n:
        header = struct.unpack_from("<I", data, i)[0]
        i += 4
        tag = header & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:            # 확장 크기: 다음 4바이트가 실제 크기
            if i + 4 > n:
                break
            size = struct.unpack_from("<I", data, i)[0]
            i += 4
        rec = data[i:i + size]
        i += size
        if tag == 67:
            out.append(_hwp_para_text(rec))
    return "\n".join(out)


def _hwp_para_text(rec: bytes) -> str:
    """문단 텍스트 레코드: UTF-16LE + 제어문자. 제어문자는 걸러낸다."""
    out, i, n = [], 0, len(rec)
    while i + 2 <= n:
        c = rec[i] | (rec[i + 1] << 8)
        if c in (10, 13):            # 줄/문단 바꿈
            out.append("\n")
            i += 2
        elif c == 0:
            i += 2
        elif c < 32:                 # 확장·인라인 컨트롤: 8워드(16바이트) 차지
            i += 16
        else:
            out.append(chr(c))
            i += 2
    return "".join(out)


def _extract_docx(src) -> str:
    from docx import Document
    return _collapse("\n".join(p.text for p in Document(src).paragraphs))


def _extract_pdf(src) -> str:
    from pypdf import PdfReader

    def _page_text(page) -> str:
        # layout 모드는 줄바꿈과 들여쓰기를 살려준다.
        # 기본 모드는 표가 한 줄로 뭉개져 참고 자료로 쓸 수 없다.
        try:
            text = page.extract_text(extraction_mode="layout")
        except Exception:
            text = None
        if not text or not text.strip():
            text = page.extract_text() or ""
        # layout 모드가 만드는 과도한 공백 정리 (들여쓰기는 유지)
        text = re.sub(r"[ \t]{3,}", "  ", text)
        return "\n".join(line.rstrip() for line in text.splitlines())

    pages = [
        f"--- {i + 1}페이지 ---\n{_page_text(p)}"
        for i, p in enumerate(PdfReader(src).pages)
    ]
    text = _collapse("\n".join(pages))
    if not re.sub(r"---\s*\d+페이지\s*---", "", text).strip():
        raise ExtractError(
            "PDF에서 텍스트를 찾지 못했습니다. 스캔 이미지 PDF는 지원하지 않습니다."
        )
    return text


# 엑셀 표시 형식 중 날짜인 기본 ID (ECMA-376 18.8.30)
_XLSX_DATE_FMT_IDS = set(range(14, 23)) | {45, 46, 47}
_XLSX_MAX_ROWS = 2000          # 시트당. 대용량 데이터 시트가 전체 분량을 잡아먹지 않게


def _xlsx_col(ref: str) -> int:
    """'C12' -> 2 (0부터)."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return max(n - 1, 0)


def _xlsx_date_styles(root) -> set:
    """날짜 서식이 걸린 cellXfs 인덱스."""
    custom = set()
    for el in root.iter():
        if _local(el.tag) == "numFmt":
            # 따옴표 리터럴·[색상] 같은 조건부를 떼고 y/d 가 남으면 날짜 서식
            code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", el.get("formatCode", "")).lower()
            if re.search(r"[yd]", code):
                custom.add(int(el.get("numFmtId", "-1")))
    styles = set()
    xfs = next((el for el in root if _local(el.tag) == "cellXfs"), None)
    if xfs is not None:
        for i, xf in enumerate(x for x in xfs if _local(x.tag) == "xf"):
            fid = int(xf.get("numFmtId", "0"))
            if fid in _XLSX_DATE_FMT_IDS or fid in custom:
                styles.add(i)
    return styles


def _extract_xlsx(src) -> str:
    """XLSX = zip + XML. 표준 라이브러리만 사용.

    시트마다 "[시트] 이름" 줄 뒤에 행 단위로 " | " 구분해 내보낸다.
    날짜 서식 셀은 일련번호(45678) 대신 "2025-01-21" 로 바꾼다
    (일정 파악이 인수인계의 핵심이라 숫자로 두면 쓸모가 없다).
    """
    import zipfile
    import xml.etree.ElementTree as ET
    from datetime import datetime, timedelta

    with zipfile.ZipFile(src, "r") as zf:
        names = set(zf.namelist())

        def _xml(name):
            if name not in names:
                return None
            with zf.open(name) as f:
                try:
                    return ET.parse(f).getroot()
                except ET.ParseError:
                    return None

        shared = []
        root = _xml("xl/sharedStrings.xml")
        if root is not None:
            for si in root:
                shared.append("".join(
                    t.text or "" for t in si.iter() if _local(t.tag) == "t"))

        root = _xml("xl/styles.xml")
        date_styles = _xlsx_date_styles(root) if root is not None else set()

        # 시트 이름 → 파일 경로 (workbook.xml 의 r:id 를 rels 로 풀어낸다)
        rels = {}
        root = _xml("xl/_rels/workbook.xml.rels")
        if root is not None:
            for r in root:
                target = r.get("Target", "").lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target
                rels[r.get("Id")] = target
        sheets = []
        root = _xml("xl/workbook.xml")
        if root is not None:
            for el in root.iter():
                if _local(el.tag) == "sheet":
                    rid = next((v for k, v in el.attrib.items()
                                if _local(k) == "id"), None)
                    if rid in rels:
                        sheets.append((el.get("name", ""), rels[rid]))
        if not sheets:
            sheets = [(Path(n).stem, n) for n in sorted(names)
                      if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]

        def _cell(c):
            kind = c.get("t", "")
            if kind == "inlineStr":
                return "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
            v = next((x.text for x in c if _local(x.tag) == "v"), None)
            if v is None:
                return ""
            if kind == "s":
                try:
                    return shared[int(v)]
                except (ValueError, IndexError):
                    return ""
            if kind == "b":
                return "TRUE" if v == "1" else "FALSE"
            if kind in ("", "n") and int(c.get("s", "0") or 0) in date_styles:
                try:
                    num = float(v)
                    d = datetime(1899, 12, 30) + timedelta(days=num)
                    return d.strftime("%Y-%m-%d %H:%M" if num % 1 else "%Y-%m-%d")
                except (ValueError, OverflowError):
                    return v
            if re.fullmatch(r"-?\d+\.0+", v):
                return v.split(".")[0]
            return v

        out = []
        for name, path in sheets:
            root = _xml(path)
            if root is None:
                continue
            rows = []
            for row in root.iter():
                if _local(row.tag) != "row":
                    continue
                cells = {}
                for c in row:
                    if _local(c.tag) == "c":
                        txt = _cell(c).strip()
                        if txt:
                            cells[_xlsx_col(c.get("r", "A"))] = txt.replace("\n", " ")
                if not cells:
                    continue
                rows.append(" | ".join(cells.get(i, "") for i in range(max(cells) + 1)))
                if len(rows) >= _XLSX_MAX_ROWS:
                    rows.append(f"(이하 생략 — {_XLSX_MAX_ROWS}행까지만 읽음)")
                    break
            if rows:
                out.append(f"[시트] {name}\n" + "\n".join(rows))

    text = _collapse("\n\n".join(out))
    if not text:
        raise ExtractError("엑셀 파일에서 내용을 찾지 못했습니다.")
    return text


def _html_to_text(html: str) -> str:
    import html as _html
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", html)
    html = re.sub(r"(?i)</t[dh]>", " | ", html)
    html = re.sub(r"<[^>]+>", "", html)
    return _html.unescape(html)


def _extract_eml(data: bytes) -> str:
    """메일(.eml). 보낸이·받는이·날짜·제목 + 본문. 첨부는 이름만 적는다."""
    from email import policy
    from email.parser import BytesParser

    msg = BytesParser(policy=policy.default).parsebytes(data)
    head = []
    for key, label in (("From", "보낸 사람"), ("To", "받는 사람"), ("Cc", "참조"),
                       ("Date", "날짜"), ("Subject", "제목")):
        val = msg.get(key)
        if val:
            head.append(f"{label}: {val}")

    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            body = part.get_content()
        except Exception:
            body = _decode_text(part.get_payload(decode=True) or b"")
        if part.get_content_type() == "text/html":
            body = _html_to_text(body)

    attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    if attachments:
        head.append("첨부: " + ", ".join(attachments))

    text = _collapse("\n".join(head) + "\n\n" + body)
    if not text:
        raise ExtractError("메일에서 내용을 찾지 못했습니다.")
    return text


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ExtractError("텍스트 인코딩 자동 감지에 실패했습니다.")


# ============================================================
# 진입점
# ============================================================
def extract_from_bytes(filename: str, data: bytes) -> str:
    """업로드된 바이트에서 텍스트 추출 (웹용). 전부 메모리에서 처리한다."""
    ext = Path(filename).suffix.lower()
    if ext not in HANDOVER_EXTS:
        raise ExtractError(f"지원하지 않는 파일 형식입니다: {ext or '(확장자 없음)'}")

    if ext == ".hwp":
        return _extract_hwp(data)
    if ext in (".txt", ".md", ".csv"):
        return _collapse(_decode_text(data))
    if ext == ".eml":
        return _extract_eml(data)

    buf = io.BytesIO(data)
    if ext == ".hwpx":
        return _extract_hwpx(buf)
    if ext == ".docx":
        return _extract_docx(buf)
    if ext == ".pdf":
        return _extract_pdf(buf)
    if ext == ".xlsx":
        return _extract_xlsx(buf)
    raise ExtractError(f"지원하지 않는 파일 형식입니다: {ext}")


def extract_from_path(path) -> str:
    """로컬 파일에서 텍스트 추출 (데스크톱용)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in HANDOVER_EXTS:
        raise ExtractError(f"지원하지 않는 파일 형식입니다: {ext or '(확장자 없음)'}")
    return extract_from_bytes(p.name, p.read_bytes())
