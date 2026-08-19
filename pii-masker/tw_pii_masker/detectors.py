# -*- coding: utf-8 -*-
"""台灣個資（PII）偵測器。

依據《個人資料保護法》第 2 條第 1 款，個人資料指自然人之姓名、出生年月日、
國民身分證統一編號、護照號碼、特徵、指紋、婚姻、家庭、教育、職業、病歷、醫療、
基因、性生活、健康檢查、犯罪前科、聯絡方式、財務情況、社會活動及其他得以直接
或間接方式識別該個人之資料。

本模組以三層機制偵測常見個資欄位，全程於本機執行、不連網：
  1. 正規表示式（格式比對）
  2. 檢查碼驗證（身分證、居留證、統一編號、信用卡），大幅降低誤判
  3. 前後文關鍵字（護照號碼、銀行帳號、出生日期等易誤判類型須有關鍵字才觸發）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 檢查碼驗證
# ---------------------------------------------------------------------------

# 身分證字號英文字母對應數值（戶役政規定）
_ID_LETTER_MAP = {
    "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17,
    "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22, "O": 35, "P": 23,
    "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28, "V": 29, "W": 32, "X": 30,
    "Y": 31, "Z": 33,
}
_ID_WEIGHTS = (1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1)


def validate_national_id(value: str) -> bool:
    """國民身分證統一編號（1 英文字母 + 9 數字）檢查碼驗證。"""
    if len(value) != 10 or value[0] not in _ID_LETTER_MAP:
        return False
    if value[1] not in "12":
        return False
    n = _ID_LETTER_MAP[value[0]]
    digits = [n // 10, n % 10] + [int(c) for c in value[1:]]
    return sum(d * w for d, w in zip(digits, _ID_WEIGHTS)) % 10 == 0


def validate_new_arc(value: str) -> bool:
    """新式外來人口統一證號（1 英文字母 + 8/9 開頭 9 數字），檢查碼同身分證。"""
    if len(value) != 10 or value[0] not in _ID_LETTER_MAP:
        return False
    if value[1] not in "89":
        return False
    n = _ID_LETTER_MAP[value[0]]
    digits = [n // 10, n % 10] + [int(c) for c in value[1:]]
    return sum(d * w for d, w in zip(digits, _ID_WEIGHTS)) % 10 == 0


def validate_old_arc(value: str) -> bool:
    """舊式居留證統一證號（2 英文字母 + 8 數字）。"""
    if len(value) != 10:
        return False
    if value[0] not in _ID_LETTER_MAP or value[1] not in "ABCD":
        return False
    n1 = _ID_LETTER_MAP[value[0]]
    n2 = _ID_LETTER_MAP[value[1]]
    digits = [n1 // 10, n1 % 10, n2 % 10] + [int(c) for c in value[2:]]
    return sum(d * w for d, w in zip(digits, _ID_WEIGHTS)) % 10 == 0


def validate_ubn(value: str) -> bool:
    """營利事業統一編號（8 位數字），採 2023 年新版檢核邏輯（餘 5）。"""
    if len(value) != 8 or not value.isdigit():
        return False
    weights = (1, 2, 1, 2, 1, 2, 4, 1)
    total = 0
    for c, w in zip(value, weights):
        p = int(c) * w
        total += p // 10 + p % 10
    if total % 5 == 0:
        return True
    # 第 7 位為 7 時，乘積 7*4=28 → 2+8=10，亦可視為 1+0=1，容許兩種計法
    return value[6] == "7" and (total + 1) % 5 == 0


def validate_luhn(value: str) -> bool:
    """信用卡卡號 Luhn 檢查。"""
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) != 16:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------------------
# 姓名偵測用資料
# ---------------------------------------------------------------------------

# 台灣常見姓氏（涵蓋人口 99% 以上之單姓）
_SURNAMES_SINGLE = (
    "陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴周徐蘇葉莊呂江何蕭羅高潘簡朱鍾彭游"
    "詹胡施沈余趙盧梁顏柯翁魏孫戴范宋方鄧杜傅侯曹薛丁卓阮馬董唐藍石蔣古紀姚連"
    "馮歐程湯田康姜汪白鄒尤巫鐘黎涂龔嚴韓袁金童陸夏柳凌邵溫倪凃俞聶原毛谷祝申"
    "甘秦史駱倩賀龎粘遊利安區辛易武韋雲樊岳虞夔隋商滕畢左權章繆花舒喬桂官塗全"
)
_SURNAMES_COMPOUND = ("歐陽", "司馬", "司徒", "諸葛", "上官", "張簡", "范姜", "東方", "周黃", "江謝")

# 以姓氏開頭、但實為一般詞彙者，避免誤遮
_NAME_STOPWORDS = {
    "陳述", "陳情", "陳報", "林地", "林業", "黃金", "黃色", "張貼", "王國", "吳郭魚",
    "劉海", "楊桃", "許可", "許多", "謝謝", "謝絕", "郭魚", "洪水", "曾經", "賴帳",
    "周知", "周圍", "周全", "徐行", "蘇打", "葉片", "莊園", "呂宋", "江山", "江湖",
    "何時", "何種", "何以", "何況", "蕭條", "羅列", "高雄", "高興", "高於", "高低",
    "潘朵拉", "簡介", "簡稱", "簡易", "朱紅", "游泳", "詹姆", "胡亂", "施工", "施行",
    "沈默", "余額", "趙錢", "盧溝", "梁柱", "顏色", "柯南", "翁婿", "魏晉", "孫子",
    "戴上", "范例", "宋朝", "方式", "方案", "方向", "方法", "鄧白氏", "杜絕", "傅立葉",
    "侯爵", "曹操", "薛丁格", "石頭", "石油", "金額", "金融", "金錢", "金門", "馬上",
    "馬路", "董事", "唐朝", "藍色", "蔣公", "古代", "紀錄", "紀念", "姚明", "連絡",
    "連結", "連續", "馮京", "歐洲", "程式", "程序", "湯匙", "田地", "康復", "姜餅",
    "汪洋", "白色", "白天", "白紙", "鄒族", "巫術", "鐘錶", "黎明", "龔自珍", "嚴重",
    "嚴格", "韓國", "袁世凱", "童年", "陸地", "夏天", "柳樹", "凌晨", "邵氏", "溫度",
    "溫暖", "倪端", "俞允", "聶耳", "原因", "原則", "毛毯", "谷歌", "祝福", "申請",
    "申報", "甘蔗", "秦朝", "史料", "駱駝", "賀卡", "利息", "利率", "安全", "安排",
    "區域", "辛苦", "易於", "武器", "韋伯", "雲端", "樊籠", "岳父", "虞美人", "隋朝",
    "商業", "商品", "滕王", "畢業", "左邊", "權利", "章節", "繆思", "花費", "花蓮",
    "舒適", "喬遷", "桂冠", "官方", "塗改", "全部", "全額", "全體",
}

_SURNAME_CLASS = "[" + _SURNAMES_SINGLE + "]"
_SURNAME_ALT = "(?:" + "|".join(_SURNAMES_COMPOUND) + "|" + _SURNAME_CLASS + ")"

# 觸發姓名偵測的欄位標籤（含保險業常用欄位）
_NAME_LABELS = (
    "姓名|收件人|申請人|當事人|聯絡人|負責人|受文者|立書人|立契約書人|要保人|"
    "被保險人|受益人|保戶|業務員|經辦人|承辦人|代理人|法定代理人|受款人|借款人|"
    "保證人|客戶|病患|患者|員工"
)


def _validate_person_name(value: str) -> bool:
    # 「陳述意見」這類以停用詞開頭的詞組也一併排除
    return not any(value.startswith(w) for w in _NAME_STOPWORDS)


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PIIMatch:
    """一筆偵測到的個資。"""
    type: str      # 機器名稱（如 national_id）
    label: str     # 中文名稱（如 身分證字號）
    start: int
    end: int
    text: str
    priority: int = 100


@dataclass(frozen=True)
class Detector:
    name: str
    label: str
    pattern: "re.Pattern[str]"
    priority: int                      # 數字越小優先權越高（重疊時保留）
    validator: Optional[Callable[[str], bool]] = None
    # 前後文關鍵字（任一命中才視為個資）；None 表示不需要前後文
    context: Optional[Tuple[str, ...]] = None
    context_before: int = 20           # 往前看的字元數
    context_after: int = 8             # 往後看的字元數
    group: int = 0                     # 實際要遮罩的群組


def _has_context(text: str, start: int, end: int,
                 keywords: Sequence[str], before: int, after: int,
                 hint: str = "") -> bool:
    """檢查比對位置前後（或外部提示文字，如表格的欄位標題、左側儲存格）
    是否出現關鍵字。"""
    window = text[max(0, start - before):start] + text[end:end + after]
    if hint:
        window += "\n" + hint
    return any(k in window for k in keywords)


# ---------------------------------------------------------------------------
# 各類型偵測器定義
# ---------------------------------------------------------------------------

# 台灣地址：縣市 + (鄉鎮市區) + (村里鄰) + (路街段) + (巷弄) + 號 + (樓/室)
_CITY = (
    r"(?:[臺台]北市|新北市|桃園市|[臺台]中市|[臺台]南市|高雄市|基隆市|新竹市|新竹縣|"
    r"嘉義市|嘉義縣|苗栗縣|彰化縣|南投縣|雲林縣|屏東縣|宜蘭縣|花蓮縣|[臺台]東縣|"
    r"澎湖縣|金門縣|連江縣)"
)
_DIST = r"(?:[一-鿿]{1,3}[區鄉鎮市])?"
_VILLAGE = r"(?:[一-鿿]{1,4}[村里])?(?:[0-9０-９]{1,3}鄰)?"
_ROAD = r"(?:[一-鿿0-9０-９]{1,10}(?:路|街|大道)(?:[0-9０-９一二三四五六七八九十]{1,3}段)?)?"
_LANE = r"(?:[0-9０-９]{1,4}巷)?(?:[0-9０-９]{1,4}弄)?"
_NUMBER = r"[0-9０-９]{1,5}(?:之[0-9０-９]{1,3})?號"
_FLOOR = r"(?:[0-9０-９]{1,3}樓(?:之[0-9０-９]{1,3})?)?(?:[0-9０-９]{1,4}室)?"
_ADDRESS_RE = _CITY + _DIST + _VILLAGE + _ROAD + _LANE + _NUMBER + _FLOOR

# 出生日期（民國 / 西元 / 數字分隔）
_DATE_ROC = r"民國\s*[0-9０-９]{1,3}\s*年\s*[0-9０-９]{1,2}\s*月\s*[0-9０-９]{1,2}\s*日"
_DATE_CJK = r"[0-9０-９]{2,4}\s*年\s*[0-9０-９]{1,2}\s*月\s*[0-9０-９]{1,2}\s*日"
_DATE_NUM = r"(?<![\d.])[0-9]{2,4}[./-][0-9]{1,2}[./-][0-9]{1,2}(?![\d.])"
_BIRTH_KEYWORDS = ("出生", "生日", "生於", "出生年月日", "誕生", "birth", "Birth", "DOB")

DETECTORS: List[Detector] = [
    Detector(
        name="national_id", label="身分證字號", priority=10,
        pattern=re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?!\d)"),
        validator=validate_national_id,
    ),
    Detector(
        name="arc_id", label="居留證號", priority=11,
        pattern=re.compile(r"(?<![A-Za-z0-9])[A-Z](?:[89]\d{8}|[A-D]\d{8})(?![A-Za-z0-9])"),
        validator=lambda v: validate_new_arc(v) or validate_old_arc(v),
    ),
    Detector(
        name="credit_card", label="信用卡號", priority=15,
        pattern=re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d)|(?<!\d)\d{16}(?!\d)"),
        validator=validate_luhn,
    ),
    Detector(
        name="nhi_card", label="健保卡號", priority=16,
        pattern=re.compile(r"(?<!\d)0000\d{8}(?!\d)"),
    ),
    Detector(
        name="passport", label="護照號碼", priority=17,
        pattern=re.compile(r"(?<![A-Za-z0-9])\d{8,9}(?!\d)"),
        context=("護照", "Passport", "passport", "PASSPORT"),
        context_before=25,
    ),
    Detector(
        name="ubn", label="統一編號", priority=18,
        pattern=re.compile(r"(?<!\d)\d{8}(?!\d)"),
        validator=validate_ubn,
        context=("統編", "統一編號", "營利事業"),
        context_before=25,
    ),
    Detector(
        name="bank_account", label="銀行帳號", priority=25,
        pattern=re.compile(r"(?<!\d)\d{3,4}(?:[ -]?\d{2,6}){2,4}(?!\d)"),
        validator=lambda v: 10 <= sum(c.isdigit() for c in v) <= 16,
        context=("帳號", "帳戶", "匯款", "轉帳", "存摺", "虛擬帳號", "銀行"),
        context_before=25,
    ),
    Detector(
        name="mobile", label="行動電話", priority=20,
        pattern=re.compile(r"(?<![\dA-Za-z+])09\d{2}[ -]?\d{3}[ -]?\d{3}(?!\d)|(?<![\dA-Za-z])\+886[ -]?9\d{2}[ -]?\d{3}[ -]?\d{3}(?!\d)"),
    ),
    Detector(
        name="landline", label="市內電話", priority=21,
        pattern=re.compile(r"(?<!\d)(?:\(0(?!9)\d{1,3}\)|0(?!9)\d{1,3}[ -])\s?\d{3,4}[ -]?\d{4}(?!\d)"),
    ),
    Detector(
        name="email", label="電子郵件", priority=22,
        pattern=re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    ),
    Detector(
        name="birthdate", label="出生日期", priority=30,
        pattern=re.compile("|".join((_DATE_ROC, _DATE_CJK, _DATE_NUM))),
        context=_BIRTH_KEYWORDS,
        context_before=25, context_after=10,
    ),
    Detector(
        name="address", label="地址", priority=40,
        pattern=re.compile(_ADDRESS_RE),
    ),
    Detector(
        name="plate", label="車牌號碼", priority=45,
        pattern=re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{2,3}-\d{3,4}|\d{3,4}-[A-Z]{2,3})(?![A-Za-z0-9])"),
        context=("車牌", "車號", "牌照", "汽車", "機車", "車輛"),
        context_before=25,
    ),
    Detector(
        name="name", label="姓名(欄位)", priority=50,
        pattern=re.compile(
            "(?:" + _NAME_LABELS + r")\s*[:：]?\s*(" + _SURNAME_ALT + r"[一-鿿]{1,3})"
        ),
        validator=_validate_person_name,
        group=1,
    ),
    Detector(
        name="name_honorific", label="姓名(稱謂)", priority=51,
        pattern=re.compile(_SURNAME_ALT + r"[一-鿿]{0,2}(?=先生|小姐|女士)"),
        validator=_validate_person_name,
    ),
    Detector(
        # 整格內容就是一個姓名（表單／試算表常見：標籤在左欄或表頭，值在另一格）
        # 僅在外部提示文字（context_hint）含姓名相關標籤時觸發
        name="name_bare", label="姓名(表格)", priority=52,
        pattern=re.compile(r"\A\s*(" + _SURNAME_ALT + r"[一-鿿]{1,3})\s*\Z"),
        validator=_validate_person_name,
        context=tuple(_NAME_LABELS.split("|")),
        context_before=0, context_after=0,
        group=1,
    ),
]

ALL_TYPES = {d.name: d.label for d in DETECTORS}


# ---------------------------------------------------------------------------
# 掃描
# ---------------------------------------------------------------------------

def scan(text: str,
         enabled: Optional[Sequence[str]] = None,
         all_dates: bool = False,
         context_hint: str = "") -> List[PIIMatch]:
    """掃描文字，回傳去重疊後的個資清單（依出現位置排序）。

    enabled:      要啟用的類型（None = 全部）
    all_dates:    True 時出生日期不需前後文關鍵字（所有完整日期一律視為個資）
    context_hint: 外部前後文（如試算表的欄位標題、表單左側儲存格文字），
                  供需要關鍵字的類型判斷；不參與遮罩本身
    """
    if not text:
        return []
    matches: List[PIIMatch] = []
    for det in DETECTORS:
        if enabled is not None and det.name not in enabled:
            continue
        for m in det.pattern.finditer(text):
            g = det.group if det.group and m.group(det.group) is not None else 0
            value = m.group(g)
            start, end = m.span(g)
            if det.validator is not None and not det.validator(value):
                continue
            ctx = det.context
            if det.name == "birthdate" and all_dates:
                ctx = None
            if ctx is not None and not _has_context(
                    text, start, end, ctx, det.context_before, det.context_after,
                    hint=context_hint):
                continue
            matches.append(PIIMatch(det.name, det.label, start, end, value, det.priority))
    return _resolve_overlaps(matches)


def _resolve_overlaps(matches: List[PIIMatch]) -> List[PIIMatch]:
    """重疊時保留優先權高（數字小）、範圍長者。"""
    ordered = sorted(matches, key=lambda m: (m.priority, -(m.end - m.start), m.start))
    kept: List[PIIMatch] = []
    for m in ordered:
        if any(m.start < k.end and k.start < m.end for k in kept):
            continue
        kept.append(m)
    kept.sort(key=lambda m: m.start)
    return kept
