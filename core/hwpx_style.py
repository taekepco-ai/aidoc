# -*- coding: utf-8 -*-
"""평문 텍스트 → 사내 보고서 서식의 HWPX(한글 문서) 바이트 생성.

kanana-doc(사내 문서 AI)의 hwpx.py 를 옮겨 온 것이다. 한글에서 글꼴·여백·
제목 상자·표 모양을 여러 차례 실제로 열어 보며 맞춘 값이므로, 서식을 바꿀 때는
아래 설정값만 고치고 XML 구조는 건드리지 않는 편이 안전하다.

core/hwpx.py 가 서식 원본(.hwpx)이 없을 때 이 모듈로 문서를 만든다.

HWPX 는 zip 안에 OWPML(XML) 이 들어 있는 형식이다. 외부 라이브러리 없이
표준 라이브러리(zipfile)만으로 최소 구성의 문서를 만든다.

zip 구성
    mimetype                  (압축하지 않고 맨 앞에 저장)
    version.xml
    META-INF/container.xml
    META-INF/manifest.xml
    Contents/content.hpf      (패키지 목록)
    Contents/header.xml       (글꼴·글자모양·문단모양·스타일 정의)
    Contents/section0.xml     (본문)
    settings.xml
"""
import io
import re
import zipfile

MIMETYPE = "application/hwp+zip"

# ── 제목 서식 (여기만 고치면 모양이 바뀝니다) ──────────────────────────────
# "none"  제목도 본문과 같은 글자로 (사각형 없음)
# "table" 1칸 표에 넣는다. 가로로 꽉 차고 배경이 깔린다  ← 현재 설정
# "box"   문단 테두리로 감싼다
# 공문(수신·제목·내부결재로 시작)에는 어떤 설정이든 제목 상자를 만들지 않는다.
TITLE_STYLE = "table"

# ── 글꼴·글자 크기 (여기만 고치면 됩니다) ──────────────────────────────────
# 크기는 100 = 1pt. 글꼴은 문서를 여는 PC 에 깔려 있어야 한다.
# 없는 글꼴이면 한글이 비슷한 글꼴로 바꿔 보여준다.
# 기본값은 사내 보고서 관례(제목·소제목 헤드라인체, 본문·표 명조체)를 따른다.
TITLE_FONT = "HY헤드라인M"  # 제목
TITLE_SIZE = 2200          # 22pt
HEADING_FONT = "HY헤드라인M"  # 소제목 "1. 추진 배경"
HEADING_SIZE = 1700        # 17pt
BODY_FONT = "휴먼명조"      # 본문 (□ ○ 줄)
BODY_SIZE = 1500           # 15pt
BODY_CHAR_SPACING = -5     # 본문 자간(%). 줄이 덜 넘어가게 살짝 좁힌다. 0 이면 기본
TABLE_FONT = "휴먼명조"     # 표 안의 글자
TABLE_SIZE = 1200          # 12pt
LINE_SPACING = 180         # 본문·소제목 줄 간격(%). 한글 [문단 모양]의 줄 간격
TABLE_LINE_SPACING = 130   # 표 칸과 제목 상자 안의 줄 간격(%)
HEADING_GAP = 1000         # 소제목 위 여백. 빈 줄은 없애고 이 값으로만 간격을 준다

TITLE_BG = "#E7E7E7"   # 제목 칸 배경색(옅은 회색, 표 머리글과 같은 색). "" 이면 흰 바탕
TITLE_SHADOW = True    # 제목 칸 오른쪽·아래를 두껍게 해서 그림자처럼 보이게 한다
TITLE_LINE_WIDTH = "0.25 mm"    # 제목 상자 위·왼쪽 테두리. 한글이 받는 값: 0.12 0.15 0.2 0.25 0.3 0.4 mm
TITLE_SHADOW_WIDTH = "0.7 mm"   # 그림자 두께. 한글이 받는 값: 0.5 0.6 0.7 1.0 1.5 2.0 mm
TITLE_BOLD = False      # 제목 굵게. HY헤드라인M 은 원래 굵은 글꼴이라 끄면 조금 가늘어진다
HEADING_BOLD = False    # 소제목("1. 추진 배경") 굵게

# ── 쪽 여백 (mm) ─────────────────────────────────────────────────────────
# 한글 [쪽 여백] 과 같은 값이다. A4 세로 기준.
MARGIN_TOP = 10        # 위쪽
MARGIN_BOTTOM = 10     # 아래쪽
MARGIN_LEFT = 18       # 왼쪽
MARGIN_RIGHT = 18      # 오른쪽
MARGIN_HEADER = 8     # 머리말
MARGIN_FOOTER = 8     # 꼬리말


def _mm(v) -> int:
    """mm → 한글 내부 단위(HWPUNIT). 1mm ≈ 283.46."""
    return int(round(v * 7200 / 25.4))


PAPER_WIDTH = 59528    # A4 너비(210mm)
# 본문 폭. 제목 상자와 표를 이 폭에 맞춰 꽉 채운다.
TEXT_WIDTH = PAPER_WIDTH - _mm(MARGIN_LEFT) - _mm(MARGIN_RIGHT)
TITLE_BOX_HEIGHT = TITLE_SIZE + 1600   # 제목 칸 높이. 글자 위아래로 여유를 둔다

VERSION_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version"\
 tagetApplication="WORDPROCESSOR" major="5" minor="1" micro="1" buildNumber="0"\
 os="1" xmlVersion="1.4" application="Hancom Office Hangul"\
 appVersion="9, 1, 1, 5656 WIN32LEWindows_Unknown_Version"/>"""

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container"\
 xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf">
<ocf:rootfiles>
<ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>
</ocf:rootfiles>
</ocf:container>"""

MANIFEST_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<odf:manifest xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" version="1.2">
<odf:file-entry full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>
<odf:file-entry full-path="Contents/header.xml" media-type="application/xml"/>
<odf:file-entry full-path="Contents/section0.xml" media-type="application/xml"/>
<odf:file-entry full-path="settings.xml" media-type="application/xml"/>
</odf:manifest>"""

SETTINGS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ha:HWPApplicationSetting xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app"\
 xmlns:config="http://www.hancom.co.kr/hwpml/2011/config-item">
<ha:CaretPosition listIDRef="0" paraIDRef="0" pos="0"/>
</ha:HWPApplicationSetting>"""

CONTENT_HPF = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<opf:package xmlns:opf="http://www.idpf.org/2007/opf/"\
 xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app"\
 xmlns:dc="http://purl.org/dc/elements/1.1/" version="" unique-identifier="" id="">
<opf:metadata>
<opf:title>{title}</opf:title>
<opf:language>ko</opf:language>
<opf:meta name="creator" content=""/>
</opf:metadata>
<opf:manifest>
<opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>
<opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>
<opf:item id="settings" href="settings.xml" media-type="application/xml"/>
</opf:manifest>
<opf:spine>
<opf:itemref idref="header" linear="yes"/>
<opf:itemref idref="section0" linear="yes"/>
</opf:spine>
</opf:package>"""


# 문서에 쓰는 글꼴 목록(겹치지 않게). 한글 파일은 글꼴을 번호로 가리킨다.
_FONTS = list(dict.fromkeys([BODY_FONT, TITLE_FONT, HEADING_FONT, TABLE_FONT]))


def _font_id(face: str) -> int:
    return _FONTS.index(face)


def _fontface(lang):
    """언어별 글꼴 정의. 위 설정에 적힌 글꼴을 모두 등록한다."""
    fonts = "".join(
        f'<hh:font id="{i}" face="{face}" type="TTF" isEmbedded="0">'
        f'<hh:typeInfo familyType="'
        f'{"FCAT_GOTHIC" if ("고딕" in face or "돋움" in face or "헤드라인" in face) else "FCAT_MYUNGJO"}"'
        f' weight="0" proportion="0" contrast="0" strokeVariation="0" armStyle="0"'
        f' letterform="0" midline="0" xHeight="0"/></hh:font>'
        for i, face in enumerate(_FONTS)
    )
    return f'<hh:fontface lang="{lang}" fontCnt="{len(_FONTS)}">{fonts}</hh:fontface>'


def _font_ref(face: str) -> str:
    i = _font_id(face)
    return (f'<hh:fontRef hangul="{i}" latin="{i}" hanja="{i}" japanese="{i}"'
            f' other="{i}" symbol="{i}" user="{i}"/>')


_LANGS = ("HANGUL", "LATIN", "HANJA", "JAPANESE", "OTHER", "SYMBOL", "USER")
_RATIO = ('<hh:ratio hangul="100" latin="100" hanja="100" japanese="100"'
          ' other="100" symbol="100" user="100"/>')
_SPACING = ('<hh:spacing hangul="0" latin="0" hanja="0" japanese="0"'
            ' other="0" symbol="0" user="0"/>')


def _spacing(v: int) -> str:
    """자간(%). 음수면 글자 사이가 좁아진다. 한글 [글자 모양]의 자간과 같다."""
    return (f'<hh:spacing hangul="{v}" latin="{v}" hanja="{v}" japanese="{v}"'
            f' other="{v}" symbol="{v}" user="{v}"/>')
_RELSZ = ('<hh:relSz hangul="100" latin="100" hanja="100" japanese="100"'
          ' other="100" symbol="100" user="100"/>')
_OFFSET = ('<hh:offset hangul="0" latin="0" hanja="0" japanese="0"'
           ' other="0" symbol="0" user="0"/>')

_BORDER_NONE = ('<hh:leftBorder type="NONE" width="0.1 mm" color="#000000"/>'
                '<hh:rightBorder type="NONE" width="0.1 mm" color="#000000"/>'
                '<hh:topBorder type="NONE" width="0.1 mm" color="#000000"/>'
                '<hh:bottomBorder type="NONE" width="0.1 mm" color="#000000"/>'
                '<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>')

# 제목을 감쌀 사각 테두리(네 변 실선) + 옅은 회색 배경
_BORDER_BOX = ('<hh:leftBorder type="SOLID" width="0.4 mm" color="#000000"/>'
               '<hh:rightBorder type="SOLID" width="0.4 mm" color="#000000"/>'
               '<hh:topBorder type="SOLID" width="0.4 mm" color="#000000"/>'
               '<hh:bottomBorder type="SOLID" width="0.4 mm" color="#000000"/>'
               '<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>')

# 그림자처럼 보이는 제목 테두리: 위·왼쪽은 보통, 오른쪽·아래는 두껍게.
# 사내 보고서 제목 상자의 모양을 따른다.
_BORDER_SHADOW = (f'<hh:leftBorder type="SOLID" width="{TITLE_LINE_WIDTH}" color="#000000"/>'
                  f'<hh:rightBorder type="SOLID" width="{TITLE_SHADOW_WIDTH}" color="#000000"/>'
                  f'<hh:topBorder type="SOLID" width="{TITLE_LINE_WIDTH}" color="#000000"/>'
                  f'<hh:bottomBorder type="SOLID" width="{TITLE_SHADOW_WIDTH}" color="#000000"/>'
                  '<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>')
_BORDER_TITLE = _BORDER_SHADOW if TITLE_SHADOW else _BORDER_BOX

# 칸 채우기. winBrush 의 faceColor 가 배경색이다.
_TITLE_FILL = (f'<hc:fillBrush><hc:winBrush faceColor="{TITLE_BG}"'
               f' hatchColor="#999999" alpha="0"/></hc:fillBrush>') if TITLE_BG else ""

# 표 칸 테두리(네 변 가는 실선)와 머리글 줄 배경(옅은 회색)
_BORDER_CELL = ('<hh:leftBorder type="SOLID" width="0.12 mm" color="#000000"/>'
                '<hh:rightBorder type="SOLID" width="0.12 mm" color="#000000"/>'
                '<hh:topBorder type="SOLID" width="0.12 mm" color="#000000"/>'
                '<hh:bottomBorder type="SOLID" width="0.12 mm" color="#000000"/>'
                '<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>')
_HEAD_FILL = ('<hc:fillBrush><hc:winBrush faceColor="#E7E7E7"'
              ' hatchColor="#999999" alpha="0"/></hc:fillBrush>')

# 개요 번호 모양(1. → 가. → 1) …). 본문에서 직접 쓰진 않지만 secPr 이 참조한다.
_PARA_HEADS = "".join(
    f'<hh:paraHead start="1" level="{lv}" align="LEFT" useInstWidth="1"'
    f' autoIndent="1" widthAdjust="0" textOffsetType="PERCENT" textOffset="50"'
    f' numFormat="DIGIT" charPrIDRef="4294967295" checkable="0">^{lv}.</hh:paraHead>'
    for lv in range(1, 8)
)

HEADER_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"\
 xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" version="1.4" secCnt="1">
<hh:beginNum page="1" footnote="1" endnote="1" pic="1" tbl="1" equation="1"/>
<hh:refList>
<hh:fontfaces itemCnt="{len(_LANGS)}">{''.join(_fontface(l) for l in _LANGS)}</hh:fontfaces>
<hh:borderFills itemCnt="4">
<hh:borderFill id="1" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">
<hh:slash type="NONE" Crooked="0" isCounter="0"/>
<hh:backSlash type="NONE" Crooked="0" isCounter="0"/>
{_BORDER_NONE}
</hh:borderFill>
<hh:borderFill id="2" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">
<hh:slash type="NONE" Crooked="0" isCounter="0"/>
<hh:backSlash type="NONE" Crooked="0" isCounter="0"/>
{_BORDER_TITLE}{_TITLE_FILL}
</hh:borderFill>
<hh:borderFill id="3" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">
<hh:slash type="NONE" Crooked="0" isCounter="0"/>
<hh:backSlash type="NONE" Crooked="0" isCounter="0"/>
{_BORDER_CELL}
</hh:borderFill>
<hh:borderFill id="4" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">
<hh:slash type="NONE" Crooked="0" isCounter="0"/>
<hh:backSlash type="NONE" Crooked="0" isCounter="0"/>
{_BORDER_CELL}{_HEAD_FILL}
</hh:borderFill>
</hh:borderFills>
<hh:charProperties itemCnt="5">
<hh:charPr id="0" height="{BODY_SIZE}" textColor="#000000" shadeColor="none"\
 useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="1">
{_font_ref(BODY_FONT)}{_RATIO}{_spacing(BODY_CHAR_SPACING)}{_RELSZ}{_OFFSET}
</hh:charPr>
<hh:charPr id="1" height="{TITLE_SIZE}" textColor="#000000" shadeColor="none"\
 useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="1">
{_font_ref(TITLE_FONT)}{_RATIO}{_SPACING}{_RELSZ}{_OFFSET}{"<hh:bold/>" if TITLE_BOLD else ""}
</hh:charPr>
<hh:charPr id="2" height="{TABLE_SIZE}" textColor="#000000" shadeColor="none"\
 useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="1">
{_font_ref(TABLE_FONT)}{_RATIO}{_SPACING}{_RELSZ}{_OFFSET}<hh:bold/>
</hh:charPr>
<hh:charPr id="3" height="{TABLE_SIZE}" textColor="#000000" shadeColor="none"\
 useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="1">
{_font_ref(TABLE_FONT)}{_RATIO}{_SPACING}{_RELSZ}{_OFFSET}
</hh:charPr>
<hh:charPr id="4" height="{HEADING_SIZE}" textColor="#000000" shadeColor="none"\
 useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="1">
{_font_ref(HEADING_FONT)}{_RATIO}{_SPACING}{_RELSZ}{_OFFSET}{"<hh:bold/>" if HEADING_BOLD else ""}
</hh:charPr>
</hh:charProperties>
<hh:tabProperties itemCnt="1">
<hh:tabPr id="0" autoTabLeft="0" autoTabRight="0"/>
</hh:tabProperties>
<hh:numberings itemCnt="1">
<hh:numbering id="1" start="0">{_PARA_HEADS}</hh:numbering>
</hh:numberings>
<hh:paraProperties itemCnt="6">
<hh:paraPr id="0" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1"\
 suppressLineNumbers="0" checked="0">
<hh:align horizontal="JUSTIFY" vertical="BASELINE"/>
<hh:heading type="NONE" idRef="0" level="0"/>
<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="KEEP_WORD"\
 widowOrphan="0" keepWithNext="0" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/>
<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>
<hh:lineSpacing type="PERCENT" value="{LINE_SPACING}" unit="HWPUNIT"/>
<hh:margin>
<hc:intent value="0" unit="HWPUNIT"/>
<hc:left value="0" unit="HWPUNIT"/>
<hc:right value="0" unit="HWPUNIT"/>
<hc:prev value="0" unit="HWPUNIT"/>
<hc:next value="0" unit="HWPUNIT"/>
</hh:margin>
<hh:border borderFillIDRef="1" offsetLeft="0" offsetRight="0" offsetTop="0"\
 offsetBottom="0" connect="0" ignoreMargin="0"/>
</hh:paraPr>
<hh:paraPr id="1" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1"\
 suppressLineNumbers="0" checked="0">
<hh:align horizontal="CENTER" vertical="BASELINE"/>
<hh:heading type="NONE" idRef="0" level="0"/>
<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="KEEP_WORD"\
 widowOrphan="0" keepWithNext="1" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/>
<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>
<hh:lineSpacing type="PERCENT" value="{TABLE_LINE_SPACING}" unit="HWPUNIT"/>
<hh:margin>
<hc:intent value="0" unit="HWPUNIT"/>
<hc:left value="0" unit="HWPUNIT"/>
<hc:right value="0" unit="HWPUNIT"/>
<hc:prev value="0" unit="HWPUNIT"/>
<hc:next value="1134" unit="HWPUNIT"/>
</hh:margin>
<hh:border borderFillIDRef="2" offsetLeft="850" offsetRight="850" offsetTop="425"\
 offsetBottom="425" connect="0" ignoreMargin="0"/>
</hh:paraPr>
<hh:paraPr id="2" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1"\
 suppressLineNumbers="0" checked="0">
<hh:align horizontal="CENTER" vertical="BASELINE"/>
<hh:heading type="NONE" idRef="0" level="0"/>
<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="KEEP_WORD"\
 widowOrphan="0" keepWithNext="0" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/>
<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>
<hh:lineSpacing type="PERCENT" value="{TABLE_LINE_SPACING}" unit="HWPUNIT"/>
<hh:margin>
<hc:intent value="0" unit="HWPUNIT"/>
<hc:left value="0" unit="HWPUNIT"/>
<hc:right value="0" unit="HWPUNIT"/>
<hc:prev value="0" unit="HWPUNIT"/>
<hc:next value="0" unit="HWPUNIT"/>
</hh:margin>
<hh:border borderFillIDRef="1" offsetLeft="0" offsetRight="0" offsetTop="0"\
 offsetBottom="0" connect="0" ignoreMargin="0"/>
</hh:paraPr>
<hh:paraPr id="3" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1"\
 suppressLineNumbers="0" checked="0">
<hh:align horizontal="LEFT" vertical="BASELINE"/>
<hh:heading type="NONE" idRef="0" level="0"/>
<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="KEEP_WORD"\
 widowOrphan="0" keepWithNext="1" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/>
<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>
<hh:lineSpacing type="PERCENT" value="{LINE_SPACING}" unit="HWPUNIT"/>
<hh:margin>
<hc:intent value="0" unit="HWPUNIT"/>
<hc:left value="0" unit="HWPUNIT"/>
<hc:right value="0" unit="HWPUNIT"/>
<hc:prev value="{HEADING_GAP}" unit="HWPUNIT"/>
<hc:next value="0" unit="HWPUNIT"/>
</hh:margin>
<hh:border borderFillIDRef="1" offsetLeft="0" offsetRight="0" offsetTop="0"\
 offsetBottom="0" connect="0" ignoreMargin="0"/>
</hh:paraPr>
<hh:paraPr id="4" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1"\
 suppressLineNumbers="0" checked="0">
<hh:align horizontal="RIGHT" vertical="BASELINE"/>
<hh:heading type="NONE" idRef="0" level="0"/>
<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="KEEP_WORD"\
 widowOrphan="0" keepWithNext="0" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/>
<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>
<hh:lineSpacing type="PERCENT" value="{LINE_SPACING}" unit="HWPUNIT"/>
<hh:margin>
<hc:intent value="0" unit="HWPUNIT"/>
<hc:left value="0" unit="HWPUNIT"/>
<hc:right value="0" unit="HWPUNIT"/>
<hc:prev value="0" unit="HWPUNIT"/>
<hc:next value="0" unit="HWPUNIT"/>
</hh:margin>
<hh:border borderFillIDRef="1" offsetLeft="0" offsetRight="0" offsetTop="0"\
 offsetBottom="0" connect="0" ignoreMargin="0"/>
</hh:paraPr>
<hh:paraPr id="5" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1"\
 suppressLineNumbers="0" checked="0">
<hh:align horizontal="LEFT" vertical="BASELINE"/>
<hh:heading type="NONE" idRef="0" level="0"/>
<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="KEEP_WORD"\
 widowOrphan="0" keepWithNext="0" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/>
<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>
<hh:lineSpacing type="PERCENT" value="{TABLE_LINE_SPACING}" unit="HWPUNIT"/>
<hh:margin>
<hc:intent value="0" unit="HWPUNIT"/>
<hc:left value="0" unit="HWPUNIT"/>
<hc:right value="0" unit="HWPUNIT"/>
<hc:prev value="0" unit="HWPUNIT"/>
<hc:next value="0" unit="HWPUNIT"/>
</hh:margin>
<hh:border borderFillIDRef="1" offsetLeft="0" offsetRight="0" offsetTop="0"\
 offsetBottom="0" connect="0" ignoreMargin="0"/>
</hh:paraPr>
</hh:paraProperties>
<hh:styles itemCnt="1">
<hh:style id="0" type="PARA" name="바탕글" engName="Normal" paraPrIDRef="0"\
 charPrIDRef="0" nextStyleIDRef="0" langID="1042" lockForm="0"/>
</hh:styles>
</hh:refList>
<hh:compatibleDocument targetProgram="HWP201X">
<hh:layoutCompatibility/>
</hh:compatibleDocument>
</hh:head>"""

# 첫 문단에만 들어가는 구역 설정(용지·여백). A4 세로, 상하 20mm / 좌우 30mm.
_SEC_PR = """<hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="1134"\
 tabStop="8000" tabStopVal="4000" tabStopUnit="HWPUNIT" outlineShapeIDRef="1"\
 memoShapeIDRef="0" textVerticalWidthHead="0" masterPageCnt="0">
<hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0" strtnum="0"/>
<hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/>
<hp:visibility hideFirstHeader="0" hideFirstFooter="0" hideFirstMasterPage="0"\
 border="SHOW_ALL" fill="SHOW_ALL" hideFirstPageNum="0" hideFirstEmptyLine="0"\
 showLineNumber="0"/>
<hp:lineNumberShape restartType="0" countBy="0" distance="0" startNumber="0"/>
<hp:pagePr landscape="WIDELY" width="59528" height="84188" gutterType="LEFT_ONLY">
<hp:margin header="@HEADER@" footer="@FOOTER@" gutter="0" left="@LEFT@" right="@RIGHT@"\
 top="@TOP@" bottom="@BOTTOM@"/>
</hp:pagePr>
<hp:footNotePr>
<hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>
<hp:noteLine length="-1" type="SOLID" width="0.12 mm" color="#000000"/>
<hp:noteSpacing betweenNotes="850" belowLine="567" aboveLine="850"/>
<hp:numbering type="CONTINUOUS" newNum="1"/>
<hp:placement place="EACH_COLUMN" beneathText="0"/>
</hp:footNotePr>
<hp:endNotePr>
<hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>
<hp:noteLine length="14692344" type="SOLID" width="0.12 mm" color="#000000"/>
<hp:noteSpacing betweenNotes="0" belowLine="567" aboveLine="850"/>
<hp:numbering type="CONTINUOUS" newNum="1"/>
<hp:placement place="END_OF_DOCUMENT" beneathText="0"/>
</hp:endNotePr>
<hp:pageBorderFill type="BOTH" borderFillIDRef="1" textBorder="PAPER"\
 headerInside="0" footerInside="0" fillArea="PAPER">
<hp:offset left="1417" right="1417" top="1417" bottom="1417"/>
</hp:pageBorderFill>
</hp:secPr>
<hp:ctrl><hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="1" sameSz="1"\
 sameGap="0"/></hp:ctrl>"""
# 쪽 여백은 위 설정(mm)으로 채운다
for _k, _v in (("@TOP@", MARGIN_TOP), ("@BOTTOM@", MARGIN_BOTTOM),
               ("@LEFT@", MARGIN_LEFT), ("@RIGHT@", MARGIN_RIGHT),
               ("@HEADER@", MARGIN_HEADER), ("@FOOTER@", MARGIN_FOOTER)):
    _SEC_PR = _SEC_PR.replace(_k, str(_mm(_v)))

def _lineseg(size: int) -> str:
    """줄 배치 정보는 넣지 않는다. 한글이 파일을 열 때 직접 계산한다.

    예전에는 '한 줄짜리' 배치를 고정으로 넣었는데, 글자가 커져 줄이 넘어가면
    한글이 두 줄을 한 자리에 겹쳐 그렸다(오른쪽 끝에 글자가 뭉개져 보임).
    linesegarray 는 한글이 다시 만드는 캐시라 빼도 된다."""
    return ""


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# 공문은 "수신"·"제목" 줄로 시작한다. 이때는 제목 상자를 만들지 않는다.
_LETTER_HEAD_RE = re.compile(r"^\s*(?:수\s*신|발\s*신|제\s*목|내부결재)")


def _title_index(lines) -> int:
    """제목 상자로 만들 줄 번호. 없으면 -1.
    보고서는 첫 줄이 제목이다. 공문(수신·제목·내부결재로 시작)은 상자를 쓰지 않는다."""
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        return -1 if _LETTER_HEAD_RE.match(line) else i
    return -1


def _title_lines(text: str) -> int:
    """제목이 제목 상자에서 몇 줄이 될지 어림한다. 한글 1칸, 공백·영숫자 반 칸."""
    width = sum(0.5 if (ch == " " or ch.isascii()) else 1.0 for ch in text.strip())
    room = (TEXT_WIDTH - 1020) / TITLE_SIZE        # 상자 안쪽 폭(여백 510×2)을 글자 수로
    return max(1, -(-int(width * 100) // int(room * 100)))    # 올림


def _title_table(text: str) -> str:
    """제목을 1행 1열 표에 담는다. 한글에서 클릭해 선택·크기 조절할 수 있다.
    treatAsChar=1 이라 글자처럼 본문 흐름을 따라간다.
    제목이 길어 두 줄이 되면 상자 높이도 그만큼 늘린다(고정 높이면 아래가 잘린다)."""
    height = TITLE_BOX_HEIGHT + (_title_lines(text) - 1) * \
        int(TITLE_SIZE * TABLE_LINE_SPACING / 100)
    inner = (
        f'<hp:p id="0" paraPrIDRef="2" styleIDRef="0" pageBreak="0"'
        f' columnBreak="0" merged="0">'
        f'<hp:run charPrIDRef="1"><hp:t>{_esc(text)}</hp:t></hp:run>'
        f'{_lineseg(TITLE_SIZE)}</hp:p>'
    )
    return (
        '<hp:tbl id="1879048192" zOrder="0" numberingType="TABLE"'
        ' textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0"'
        ' dropcapstyle="None" pageBreak="CELL" repeatHeader="0"'
        ' rowCnt="1" colCnt="1" cellSpacing="0" borderFillIDRef="2" noAdjust="0">'
        f'<hp:sz width="{TEXT_WIDTH}" widthRelTo="ABSOLUTE" height="{height}"'
        ' heightRelTo="ABSOLUTE" protect="0"/>'
        '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1"'
        ' allowOverlap="0" holdAnchorAndSO="0" vertRelTo="PARA" horzRelTo="COLUMN"'
        ' vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
        '<hp:outMargin left="0" right="0" top="0" bottom="283"/>'
        '<hp:inMargin left="510" right="510" top="141" bottom="141"/>'
        '<hp:tr>'
        '<hp:tc name="" header="0" hasMargin="0" protect="0" editable="0"'
        ' dirty="0" borderFillIDRef="2">'
        '<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK"'
        ' vertAlign="CENTER" linkListIDRef="0" linkListNextIDRef="0"'
        ' textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">'
        f'{inner}'
        '</hp:subList>'
        '<hp:cellAddr colAddr="0" rowAddr="0"/>'
        '<hp:cellSpan colSpan="1" rowSpan="1"/>'
        f'<hp:cellSz width="{TEXT_WIDTH}" height="{height}"/>'
        '<hp:cellMargin left="510" right="510" top="141" bottom="141"/>'
        '</hp:tc>'
        '</hp:tr>'
        '</hp:tbl>'
    )


# ── 본문 표 ──────────────────────────────────────────────────
# 모델은 표를 "| 구분 | 내용 |" 처럼 세로줄로 쓴다. 문서에서 뽑은 표도 같은 모양이다.
# 이런 줄이 두 줄 이상 이어지면 한글 표로 바꾼다. 첫 줄은 머리글(굵게, 회색 바탕).
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
# 마크다운 표의 구분선("|---|:---:|"). 칸으로 만들지 않고 건너뛴다.
_TABLE_SEP_RE = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{0,}:?\s*\|?\s*$")
ROW_HEIGHT = TABLE_SIZE + 800   # 칸 높이(글자 + 안쪽 여백). 내용이 길면 한글이 늘린다
_TABLE_ID_BASE = 1879048300


def _is_table_row(line: str) -> bool:
    """세로줄로 시작하고 끝나며 칸이 두 개 이상인 줄."""
    return bool(_TABLE_ROW_RE.match(line)) and line.count("|") >= 3


def _split_row(line: str) -> list:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _table_rows(lines, start):
    """start 줄부터 이어지는 표를 읽는다. (칸 목록들, 다음 줄 번호)."""
    rows, i = [], start
    while i < len(lines) and _is_table_row(lines[i]):
        if not _TABLE_SEP_RE.match(lines[i]):
            rows.append(_split_row(lines[i]))
        i += 1
    return rows, i


def _cell_xml(text: str, col: int, row: int, width: int, header: bool,
              left: bool = False) -> str:
    fill, para, char = ("4", "2", "2") if header else ("3", "5" if left else "2", "3")
    return (
        f'<hp:tc name="" header="{1 if header else 0}" hasMargin="0" protect="0"'
        f' editable="0" dirty="0" borderFillIDRef="{fill}">'
        '<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK"'
        ' vertAlign="CENTER" linkListIDRef="0" linkListNextIDRef="0"'
        ' textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">'
        f'<hp:p id="0" paraPrIDRef="{para}" styleIDRef="0" pageBreak="0"'
        ' columnBreak="0" merged="0">'
        f'<hp:run charPrIDRef="{char}"><hp:t>{_esc(text)}</hp:t></hp:run>'
        f'{_lineseg(TABLE_SIZE)}</hp:p>'
        '</hp:subList>'
        f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
        '<hp:cellSpan colSpan="1" rowSpan="1"/>'
        f'<hp:cellSz width="{width}" height="{ROW_HEIGHT}"/>'
        '<hp:cellMargin left="510" right="510" top="141" bottom="141"/>'
        '</hp:tc>'
    )


# 칸 너비 비중의 하한·상한(글자 수). 짧은 칸("월", "수량")은 좁게, 긴 칸은 넓게 나누되
# 한 칸이 표를 다 차지하거나 머리글이 꺾일 만큼 좁아지지 않게 묶는다.
_COL_MIN, _COL_MAX = 4, 30
# 이보다 긴 글이 든 칸은 왼쪽 정렬한다(긴 문장을 가운데 정렬하면 읽기 어렵다)
_LEFT_ALIGN_CHARS = 14


def _col_widths(rows, ncol: int) -> list:
    """칸마다 가장 긴 글자 수에 비례해 본문 폭을 나눈다. 합은 정확히 TEXT_WIDTH."""
    weights = [min(_COL_MAX, max(_COL_MIN, max(len(r[c]) for r in rows)))
               for c in range(ncol)]
    total = sum(weights)
    widths = [TEXT_WIDTH * w // total for w in weights]
    widths[-1] += TEXT_WIDTH - sum(widths)      # 나눗셈 나머지는 마지막 칸에
    return widths


def _grid_table(rows, table_id: int) -> str:
    """칸 목록 → 한글 표. 칸 너비는 내용 길이에 비례. 모자란 칸은 빈칸으로 채운다."""
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    widths = _col_widths(rows, ncol)
    left = [max(len(r[c]) for r in rows[1:]) > _LEFT_ALIGN_CHARS if len(rows) > 1 else False
            for c in range(ncol)]
    trs = "".join(
        "<hp:tr>"
        + "".join(_cell_xml(cell, c, r, widths[c], r == 0, left[c])
                  for c, cell in enumerate(row))
        + "</hp:tr>"
        for r, row in enumerate(rows)
    )
    return (
        f'<hp:tbl id="{table_id}" zOrder="0" numberingType="TABLE"'
        ' textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0"'
        ' dropcapstyle="None" pageBreak="CELL" repeatHeader="1"'
        f' rowCnt="{len(rows)}" colCnt="{ncol}" cellSpacing="0"'
        ' borderFillIDRef="3" noAdjust="0">'
        f'<hp:sz width="{sum(widths)}" widthRelTo="ABSOLUTE"'
        f' height="{ROW_HEIGHT * len(rows)}" heightRelTo="ABSOLUTE" protect="0"/>'
        '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1"'
        ' allowOverlap="0" holdAnchorAndSO="0" vertRelTo="PARA" horzRelTo="COLUMN"'
        ' vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
        '<hp:outMargin left="0" right="0" top="0" bottom="283"/>'
        '<hp:inMargin left="510" right="510" top="141" bottom="141"/>'
        f'{trs}</hp:tbl>'
    )


def _table_paragraph(rows, index: int, table_id: int) -> str:
    """표 하나를 담은 문단. 표는 글자처럼 본문 흐름을 따라간다(treatAsChar)."""
    head = _SEC_PR if index == 0 else ""
    run = f'<hp:run charPrIDRef="0">{head}{_grid_table(rows, table_id)}</hp:run>'
    return (
        f'<hp:p id="{2147483648 + index}" paraPrIDRef="0" styleIDRef="0"'
        f' pageBreak="0" columnBreak="0" merged="0">{run}'
        f'{_lineseg(ROW_HEIGHT * len(rows))}</hp:p>'
    )


# 작성 일자 줄: "2026. 9.", "'26. 9. 14(월)" 처럼 연.월. 로 시작하는 줄.
# 제목 바로 다음에 오면 빈 줄을 없애고 오른쪽 정렬로 제목 밑에 붙인다.
_DATE_RE = re.compile(r"^\s*'?\d{2,4}\.\s*\d{1,2}\.")


# 소제목 줄: "1. 추진 배경" 처럼 한두 자리 번호 + 한글.
# "2026. 9." 같은 날짜 줄은 네 자리라 걸리지 않는다.
_HEADING_RE = re.compile(r"^\s*\d{1,2}\.\s+[가-힣]")

# 공백으로 오른쪽에 밀어 둔 줄(보고서 머리의 부서명 등). 프롬프트가 "앞에 공백을
# 충분히 넣어 오른쪽으로 밀 것" 이라 지시하므로, 공백이 이만큼 이상이면 오른쪽 정렬한다.
_RIGHT_PAD_RE = re.compile(r"^ {16,}\S")


# 항목 기호별 들여쓰기(칸 수). 모델은 공백 3·6칸으로 들여 쓰는데, 15pt 글자에서는
# 너무 안쪽으로 들어간다. 사내 보고서처럼 □ 는 한 칸, ○ 는 세 칸으로 맞춘다.
_INDENT = {"□": 1, "○": 3, "-": 5, "·": 5, "※": 1}


def _indent(text: str) -> str:
    body = text.lstrip()
    if body and body[0] in _INDENT:
        return " " * _INDENT[body[0]] + body
    return text


def _paragraph(text: str, index: int, is_title: bool = False,
               is_date: bool = False, report: bool = True) -> str:
    """한 줄 → 한 문단. 첫 문단에는 구역 설정(secPr)을 넣는다.
    제목 서식은 TITLE_STYLE 에 따른다("none"이면 본문과 똑같이 쓴다)."""
    head = _SEC_PR if index == 0 else ""
    if is_title and TITLE_STYLE == "table":
        run = f'<hp:run charPrIDRef="0">{head}{_title_table(text)}</hp:run>'
        return (
            f'<hp:p id="{2147483648 + index}" paraPrIDRef="0" styleIDRef="0"'
            f' pageBreak="0" columnBreak="0" merged="0">{run}'
            f'{_lineseg(TITLE_BOX_HEIGHT)}</hp:p>'
        )
    if is_date or (report and _RIGHT_PAD_RE.match(text)):
        text = text.strip()
        char_id, para_id, size = "0", "4", BODY_SIZE        # 제목 밑 오른쪽 정렬
    elif is_title:
        char_id, para_id, size = "1", "1", TITLE_SIZE
    elif report and _HEADING_RE.match(text):
        char_id, para_id, size = "4", "3", HEADING_SIZE     # 소제목
    else:
        char_id, para_id, size = "0", "0", BODY_SIZE
        text = _indent(text)
    run = f'<hp:run charPrIDRef="{char_id}">{head}<hp:t>{_esc(text)}</hp:t></hp:run>'
    seg = _lineseg(size)
    return (
        f'<hp:p id="{2147483648 + index}" paraPrIDRef="{para_id}" styleIDRef="0"'
        f' pageBreak="0" columnBreak="0" merged="0">{run}{seg}</hp:p>'
    )


def _section_xml(text: str, styled_title: bool = True) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines:
        lines = [""]
    styled_title = styled_title and TITLE_STYLE in ("table", "box")
    title_at = _title_index(lines) if styled_title else -1
    # 제목 다음 첫 줄이 날짜면 제목 바로 밑에 붙인다(사이 빈 줄 없앰)
    date_at = -1
    if title_at >= 0:
        k = title_at + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k < len(lines) and _DATE_RE.match(lines[k]):
            date_at = k
    # 보고서(제목 상자가 있는 문서)에만 보고서 서식을 쓴다.
    # 공문(수신·제목으로 시작)은 빈 줄이 문단 구분이고, "1. 배경" 도 본문 글자 그대로다.
    report = title_at >= 0
    parts, i, tables = [], 0, 0
    while i < len(lines):
        if title_at < i < date_at:      # 제목과 날짜 사이의 빈 줄
            i += 1
            continue
        if report and not lines[i].strip() and i != 0:
            # 보고서의 빈 줄은 버린다. 간격은 소제목 위 여백(HEADING_GAP)이 맡는다.
            # (첫 줄은 구역 설정이 들어가는 자리라 남긴다)
            i += 1
            continue
        if _is_table_row(lines[i]):
            rows, end = _table_rows(lines, i)
            if len(rows) >= 2:   # 머리글만 있는 한 줄짜리는 표로 보지 않는다
                parts.append(_table_paragraph(rows, i, _TABLE_ID_BASE + tables))
                tables += 1
                i = end
                continue
        parts.append(_paragraph(lines[i], i, i == title_at, i == date_at, report))
        i += 1
    paras = "".join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"'
        ' xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
        ' xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">'
        f"{paras}</hs:sec>"
    )


def build_styled(text: str, title: str = "문서", styled_title: bool = True) -> bytes:
    """평문 텍스트를 HWPX 파일 바이트로 만든다.

    제목 서식은 위쪽 TITLE_STYLE 로 정한다. 공문(수신·제목으로 시작)은 제목 상자 없이
    본문 글자 그대로, 보고서는 첫 줄을 제목 상자로 만든다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype 은 규격상 압축하지 않고 맨 앞에 넣어야 한다
        z.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE, zipfile.ZIP_STORED)
        z.writestr("version.xml", VERSION_XML)
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("META-INF/manifest.xml", MANIFEST_XML)
        z.writestr("Contents/content.hpf", CONTENT_HPF.format(title=_esc(title)))
        z.writestr("Contents/header.xml", HEADER_XML)
        z.writestr("Contents/section0.xml", _section_xml(text, styled_title))
        z.writestr("settings.xml", SETTINGS_XML)
    return buf.getvalue()
