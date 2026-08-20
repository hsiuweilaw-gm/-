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
    # 以下為實際名冊稽核陸續補入者（饒、崔、任等原缺，造成整筆姓名漏遮）
    "饒莫崔任柴阿宗于孔杭項竇伍卞戚岑冉嵇衛尚符祁茅龐熊屈祖景束龍幸司韶郜"
    "薊薄印宿懷蒲邰從鄂索咸籍藺屠蒙池陰鬱胥能蒼雙聞莘黨翟譚貢勞逄姬扶堵宰"
    "酈雍卻璩桑濮牛壽通邊扈燕冀郟浦農別晏瞿閻充慕茹習宦艾魚容向慎戈庾終暨"
    "居衡步都耿滿弘匡國寇廣祿殳沃蔚越隆師鞏厙晁勾敖融冷訾闞空毋沙乜養鞠須"
    "豐巢關蒯相查后荊紅竺逯蓋益桓公麥翦邢竹汲昌卲鄞介亓么"
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

# 名字不可吃進機構／職稱字樣：「楊惠萍保險經紀人」的姓名只到「楊惠萍」，
# 否則會遮成「楊○○○險經紀人」這種既走樣又仍可辨識的亂碼
_ORG_WORDS = (
    "保險", "經紀", "代理", "公司", "銀行", "事務所", "股份", "有限", "企業",
    "商行", "工作室", "分行", "分公司", "營業", "處所", "單位", "部門", "科技",
    "先生", "小姐", "女士", "經理", "襄理", "協理", "總監", "主任", "專員",
    "顧問", "業務", "襄", "的", "與", "之", "及", "和", "或",
)
_NAME_BODY = "(?:(?!" + "|".join(_ORG_WORDS) + ")[一-鿿]){1,3}"

# 觸發姓名偵測的欄位標籤（含保險業常用欄位）
_NAME_LABELS = (
    "姓名|收件人|申請人|當事人|聯絡人|負責人|受文者|立書人|立契約書人|要保人|"
    "被保險人|受益人|保戶|業務員|經辦人|承辦人|代理人|法定代理人|受款人|借款人|"
    "保證人|客戶|病患|患者|員工|推介者|輔導者|繼承人|介紹人|招攬人|推薦人|主管|"
    "主被保人|副被保人|經手人|送件人|服務人員|招攬業務員|原業務員|新業務員"
)

# 姓名欄常見的「關係稱謂」等非姓名內容，不遮
_RELATION_WORDS = {
    "父親", "母親", "配偶", "兒子", "女兒", "先生", "太太", "朋友", "同事",
    "兄弟", "姊妹", "哥哥", "弟弟", "姊姊", "妹妹", "祖父", "祖母", "外公",
    "外婆", "家人", "親屬", "本人", "同上", "無", "不詳", "未填",
}


def _validate_person_name(value: str) -> bool:
    # 「陳述意見」這類以停用詞開頭的詞組也一併排除
    return not any(value.startswith(w) for w in _NAME_STOPWORDS)


# 看起來是「欄位標題」而非姓名的詞彙（表頭儲存格、表單標籤不可誤遮）
_FIELD_WORDS = (
    "姓名", "名稱", "電話", "手機", "行動", "地址", "住址", "身分", "身份",
    "生日", "性別", "郵件", "信箱", "電子", "號碼", "日期", "帳號", "銀行",
    "統編", "編號", "代號", "狀態", "備註", "證照", "考試", "資格", "單位",
)


def _validate_bare_name(value: str) -> bool:
    if value in _RELATION_WORDS:
        return False
    if any(w in value for w in _FIELD_WORDS):
        return False
    # 姓名欄整格內容是強力訊號：停用詞採「完全相等」比對即可——
    # 用「開頭比對」會誤傷黃金燕、徐行康、王國政這類真實姓名
    return value not in _NAME_STOPWORDS


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
    # 是否採用外部提示（表格表頭／鄰格）判斷前後文。
    # 鄰格文字對「整格即個資」的類型是好訊號，但對自由文字中的類型
    # （如保單號碼）會造成鄰欄關鍵字外溢誤判，故可關閉
    use_hint: bool = True


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
# 縣市含 2010 年改制前的舊縣名（台北縣、桃園縣、台中縣、台南縣、高雄縣），
# 舊文件、戶籍謄本、保單上仍常出現
_CITY = (
    r"(?:[臺台]北市|新北市|桃園市|[臺台]中市|[臺台]南市|高雄市|基隆市|新竹市|新竹縣|"
    r"嘉義市|嘉義縣|苗栗縣|彰化縣|南投縣|雲林縣|屏東縣|宜蘭縣|花蓮縣|[臺台]東縣|"
    r"澎湖縣|金門縣|連江縣|[臺台]北縣|桃園縣|[臺台]中縣|[臺台]南縣|高雄縣)"
)
_DIST = r"(?:[一-鿿]{1,3}?[區鄉鎮市])?"  # 懶惰比對，避免吃掉「市府路」的「市」
_VILLAGE = r"(?:[一-鿿]{1,4}[村里])?(?:[0-9０-９]{1,3}鄰)?"
_ROAD = r"(?:[一-鿿0-9０-９]{1,10}(?:路|街|大道)(?:[0-9０-９一二三四五六七八九十]{1,3}段)?)?"
_LANE = r"(?:[0-9０-９]{1,4}巷)?(?:[0-9０-９]{1,4}弄)?"
_NUMBER = r"[0-9０-９]{1,5}(?:[之\-－][0-9０-９]{1,3})?號"
_FLOOR = (
    r"(?:[0-9０-９一二三四五六七八九十]{1,3}\s?(?:樓|[Ff])(?:之[0-9０-９]{1,3})?)?"
    r"(?:[0-9０-９]{1,4}室)?"
)
_ADDRESS_RE = _CITY + _DIST + _VILLAGE + _ROAD + _LANE + _NUMBER + _FLOOR

# 未寫縣市的地址（例：板橋區文化路一段23號5樓）：
# 從行政區/道路層級開始比對，須搭配「地址」類關鍵字或表格提示才觸發，避免誤判
_ADDR_NO_CITY_RE = (
    r"[一-鿿]{1,4}[區鄉鎮市村里]?" + _VILLAGE + _ROAD + _LANE + _NUMBER + _FLOOR
)
_ADDR_LABELS = ("地址", "住址", "戶籍", "居所", "住居所", "通訊處", "居住地",
                "通訊地址", "聯絡地址", "寄送地址", "營業地址", "地點")


def _validate_no_city_address(value: str) -> bool:
    # 必須含道路／巷弄／行政區層級字樣，排除「編號123號」這類非地址內容
    return any(k in value for k in ("路", "街", "大道", "巷", "弄", "區", "鄉", "鎮", "村", "里"))


def _validate_cell_address(value: str) -> bool:
    """整格地址（地址欄）驗證。地址欄常只存門牌片段（縣市、鄉鎮市區在
    其他欄位），例如「745號」「東園街73巷49號二樓」「民旅西路」，
    格式驗證放寬、由欄位提示把關。

    注意：「大安區」「台北市」這類行政區層級內容不遮（去識別化常規
    是保留到鄉鎮市區），村里與道路以下才遮。
    """
    value = value.strip()
    # 含道路／村里層級字樣（含純路名）
    if any(k in value for k in ("路", "街", "大道", "巷", "弄", "村", "里")):
        return True
    # 含門牌號
    if re.search(r"[0-9０-９一-鿿]\s*號", value):
        return True
    # 舊版遮罩截斷殘留（如「慈惠三****」：路字被星號蓋掉），重新全遮；
    # 「台北市大安區***」這類合規輸出經 _mask_address 重遮結果不變
    return re.fullmatch(r"[一-鿿]{1,6}[*＊]+", value) is not None

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
        validator=lambda v: 9 <= sum(c.isdigit() for c in v) <= 16,
        context=("帳號", "帳戶", "匯款", "轉帳", "存摺", "虛擬帳號", "銀行"),
        context_before=25,
    ),
    Detector(
        name="mobile", label="行動電話", priority=20,
        pattern=re.compile(r"(?<![\dA-Za-z+])09\d{2}[ -]?\d{3}[ -]?\d{3}(?!\d)|(?<![\dA-Za-z])\+886[ -]?9\d{2}[ -]?\d{3}[ -]?\d{3}(?!\d)"),
    ),
    Detector(
        # 三種形態：括號/分隔符號區碼（(02)2712-3456、037-123456）、
        # 無分隔符號連續數字（0223456789，Excel 匯出常見）
        name="landline", label="市內電話", priority=21,
        pattern=re.compile(
            r"(?<!\d)(?:\(0(?!9)\d{1,3}\)|0(?!9)\d{1,3}[ -])\s?\d{3,4}[ -]?\d{3,4}(?!\d)"
            r"|(?<![\dA-Za-z.+-])0(?!9)\d{8,9}(?!\d)"
        ),
        validator=lambda v: 9 <= sum(c.isdigit() for c in v) <= 10,
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
        # 未寫縣市、但前面有「地址：」等標籤的地址
        name="address_ctx", label="地址(未含縣市)", priority=41,
        pattern=re.compile(
            "(?:" + "|".join(_ADDR_LABELS) + r")\s*[:：]?\s*(" + _ADDR_NO_CITY_RE + ")"
        ),
        validator=_validate_no_city_address,
        group=1,
    ),
    Detector(
        # 整格內容是地址片段（試算表「地址」欄常見：縣市鄉鎮在其他欄位，
        # 本欄只有「745號」「東園街73巷49號二樓」等），須表格提示觸發。
        # 格式驗證放寬（由 validator 確認含門牌或道路層級字樣）
        # 整格照遮：門牌後常掛「B１」「五樓之ㄧ」「(公司名)」等不規則字尾，
        # 由 validator 確認含門牌或道路層級字樣即可
        name="address_bare", label="地址(表格)", priority=42,
        pattern=re.compile(r"\A\s*(\S[^\n]{0,78}?)\s*\Z"),
        validator=_validate_cell_address,
        context=_ADDR_LABELS,
        context_before=0, context_after=0,
        group=1,
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
            "(?:" + _NAME_LABELS + r")\s*[:：]?\s*(" + _SURNAME_ALT + _NAME_BODY + ")"
        ),
        validator=_validate_person_name,
        group=1,
    ),
    Detector(
        # 「姓名(證號)」是自由文字中最強的姓名訊號，不需任何標籤：
        # 林宥慈(F220755862)、饒培杰(A121775567)、葉珍玲(HC-20)
        # 前方加中文邊界，避免把機構名尾段誤判為姓名
        # （「祐誠行政專帳(00000002)」的「行政專帳」前接「誠」，不觸發）
        name="name_paren_id", label="姓名(附證號)", priority=49,
        pattern=re.compile(
            r"(?<![一-鿿])(" + _SURNAME_ALT + _NAME_BODY + r")"
            r"\s*[（(][A-Za-z0-9\-]{4,20}[)）]"
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
        # 整格內容就是一個姓名（表單／試算表常見：標籤在左欄或表頭，值在另一格）。
        # 僅在外部提示文字（context_hint）含姓名相關標籤時觸發；
        # 欄位標題已是強力訊號，因此「不」要求姓氏在姓氏庫內
        # （姓氏庫列不完：陶、葛、錢…都曾造成漏遮），並支援原住民名字的間隔號
        name="name_bare", label="姓名(表格)", priority=52,
        pattern=re.compile(r"\A\s*([一-鿿][一-鿿·]{1,6})\s*\Z"),
        validator=_validate_bare_name,
        context=tuple(_NAME_LABELS.split("|")),
        context_before=0, context_after=0,
        group=1,
    ),
    Detector(
        # 整格內容是識別號碼（身分證欄的統編/舊式證號/檢查碼錯誤證號、
        # 證照號碼、考試號碼、員工編號、帳號等欄），須表格提示觸發。
        # 這些欄位標題已明示內容性質，不再要求檢查碼
        name="id_bare", label="識別號碼(表格)", priority=53,
        # 允許字尾字母／狀態碼（89731134-BD、FB1001285G），
        # 括號附註（如「(HC-08)」）保留在遮罩範圍外
        pattern=re.compile(
            r"\A\s*([A-Za-z一-鿿]{0,4}[A-Za-z0-9\-]{5,20})"
            r"(?:\s*[（(][^（()）\n]{0,20}[)）])?\s*\Z"),
        validator=lambda v: sum(c.isdigit() for c in v) >= 6,
        context=("身分證", "身份證", "統一證號", "證照號碼", "考試號碼",
                 "證書字號", "員工編號", "員編", "帳號", "會員編號", "保單號碼",
                 "病歷號", "學號"),
        context_before=0, context_after=0,
        group=1,
    ),
    Detector(
        # 電話欄的非標準格式（無區碼、雙破折號、多值逗號分隔…），
        # 須表格提示或前後文含電話關鍵字才觸發
        name="phone_bare", label="電話(欄位)", priority=26,
        pattern=re.compile(r"(?<![\dA-Za-z])[0-9][0-9\- ()]{4,17}[0-9](?!\d)"),
        validator=lambda v: 7 <= sum(c.isdigit() for c in v) <= 12,
        context=("電話", "手機", "行動", "傳真", "TEL", "Tel", "tel",
                 "FAX", "Fax", "fax", "Phone", "phone", "Mobile", "mobile"),
        context_before=12,
    ),
    Detector(
        # 保單號碼／受理號碼：個資法上屬「得以間接方式識別」之個資
        # （憑此可於保險公司系統查得特定個人）。須有「保單」等關鍵字才觸發，
        # 且不採用鄰格提示，避免鄰欄關鍵字外溢誤判。
        # 若業務上需保留，可用 --exclude-types policy_no 關閉
        name="policy_no", label="保單號碼", priority=55,
        pattern=re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{8,20}(?![A-Za-z0-9])"),
        validator=lambda v: sum(c.isdigit() for c in v) >= 6,
        context=("保單", "受理", "契約", "要保書", "保額", "投保"),
        context_before=32, context_after=4,
        use_hint=False,
    ),
    Detector(
        # 備註／注意事項等自由文字欄中的長數字（帳號、證號、日期序號…），
        # 一律保守遮罩
        name="note_digits", label="備註內號碼", priority=90,
        pattern=re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{0,3}[0-9][0-9\-]{4,18}[0-9](?![A-Za-z0-9])"),
        validator=lambda v: sum(c.isdigit() for c in v) >= 7,
        context=("備註", "注意事項", "記事", "摘要", "說明"),
        context_before=0, context_after=0,
    ),
]

ALL_TYPES = {d.name: d.label for d in DETECTORS}

# 預設「不」啟用的類型：屬個資法間接識別資料，但業務上常需保留辨識，
# 由使用者自行決定是否開啟（CLI: --mask-policy-no／GUI: 勾選選項）
DEFAULT_OFF_TYPES = ("policy_no",)


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
                    hint=context_hint if det.use_hint else ""):
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
