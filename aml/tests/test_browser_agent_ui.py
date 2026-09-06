"""業務員前台的瀏覽器煙霧測試。

隱藏分數這件事的實際防線在前端 JavaScript：伺服器不回傳分數，
但畫面是否正確呈現「填答進度 → 跨越門檻警示 → 照會確認 → 開放送出」
無法由 Python 測試涵蓋。此檔曾抓出一個 render() 判斷式殘留舊欄位、
導致業務員畫面完全不更新的缺陷，故保留於測試套件中。

未安裝 Playwright 或找不到瀏覽器時自動略過。
"""
from __future__ import annotations

import os
import pathlib
import socket
import threading
import time

import pytest

from app.models import OrgUnit, Role
from app.scoring.engine import load_questionnaire
from app.services import assessments as svc

from .conftest import make_user

playwright_api = pytest.importorskip("playwright.sync_api")

PASSWORD = "correct-horse-battery"
HIGH_RISK = {
    "geo_domicile": "overseas", "geo_nationality": "foreign", "cust_occupation": "tier1",
    "cust_entity_type": "legal_entity", "cust_source": "inbound", "cust_amount": "over_5m",
    "product_type": "oiu", "txn_channel": "online", "txn_fund_source": "borrowed",
    "txn_payer": "third_party",
}


def _chromium_path() -> str | None:
    """找出可用的 Chromium。

    依序嘗試：環境變數指定的路徑、Playwright 自己解析的路徑、
    以及瀏覽器安裝目錄下任何一個 chromium 執行檔。
    最後一項是必要的：預先安裝的瀏覽器版本未必與 Playwright 套件版本相符，
    此時 Playwright 解析出的路徑並不存在。
    """
    override = os.environ.get("AML_TEST_CHROMIUM")
    if override and os.path.exists(override):
        return override

    with playwright_api.sync_playwright() as p:
        resolved = p.chromium.executable_path
    if resolved and os.path.exists(resolved):
        return resolved

    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if root and os.path.isdir(root):
        for candidate in sorted(pathlib.Path(root).glob("chromium-*/chrome-linux*/chrome"),
                                reverse=True):
            if candidate.exists():
                return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(db):
    """在背景執行緒啟動真實的 uvicorn，供瀏覽器連線。"""
    import uvicorn

    from app.db import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.skip("uvicorn 未能啟動")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    app.dependency_overrides.clear()


@pytest.fixture
def page(live_server):
    executable = _chromium_path()
    if executable is None:
        pytest.skip("找不到 Chromium，請先執行 playwright install chromium")
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=executable)
        context = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-TW")
        yield context.new_page()
        browser.close()


def _expect_text(locator, expected: str, message: str, timeout: int = 8000) -> None:
    """等到文字變成預期值為止，逾時才判定失敗。

    固定等待毫秒數的寫法在 CI 機器負載高時會偽陽性——存檔的往返還沒回來
    就檢查了。週期性的假警報比沒有測試更糟：紅燈久了就沒人看。
    """
    try:
        playwright_api.expect(locator).to_have_text(expected, timeout=timeout)
    except AssertionError as exc:
        raise AssertionError(f"{message}（等待逾時，實得「{locator.inner_text()}」）") from exc


def _login(page, base, username):
    page.goto(f"{base}/login")
    page.fill("#username", username)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def test_agent_ui_hides_score_and_gates_submit_on_consultation(db, live_server, page):
    unit = OrgUnit(code="TC01", name="台中通訊處")
    db.add(unit)
    db.commit()
    agent = make_user(db, "agent01", Role.AGENT, unit.id)
    case = svc.create_draft(db, agent)
    q = load_questionnaire()

    _login(page, live_server, "agent01")
    page.goto(f"{live_server}/assessments/{case.case_no}")
    page.wait_for_load_state("networkidle")

    crossed_at = None
    for index, factor in enumerate(q.factors, start=1):
        option = HIGH_RISK[factor.code]
        el = page.locator(f"input[name='{factor.code}'][value='{option}']")
        el.scroll_into_view_if_needed()
        el.check()
        _expect_text(page.locator("#score-count"), f"{index} / {len(q.factors)}",
                     "填答進度必須隨作答更新——若 render() 沒被呼叫，畫面會完全停住")
        if crossed_at is None and page.locator("#score-notice").inner_text().strip():
            crossed_at = index

    assert crossed_at is not None, "跨越門檻時必須出現照會警示"
    assert "須照會單位主管" in page.locator("#score-notice").inner_text()

    body = page.locator("body").inner_text()
    assert "目前總分" not in body and "門檻" not in body, "業務員畫面不得出現分數或門檻"

    assert page.locator("#submit-btn").is_disabled(), "未照會前不得送出"
    assert "照會主管確認" in page.locator("#missing-hint").inner_text()

    page.fill("#supervisor_name", "陳經理")
    page.click("#consult-btn")
    playwright_api.expect(page.locator("#submit-btn")).to_be_enabled(timeout=8000)
    assert "陳經理" in page.locator("#consult-done").inner_text()


def test_bad_premium_is_named_and_does_not_block_answers(db, live_server, page):
    """單一欄位格式錯誤，不得讓後續作答靜默丟失。

    實地測試時發生過：保費欄輸入非數字 → 伺服器回 422 → 前端無限重試同一筆，
    整個儲存佇列被堵死，十題作答全部沒進資料庫，畫面卻只顯示「請確認網路」。
    業務員在客戶面前只會判斷成系統故障，這道法遵管制在現場等於失效。
    """
    unit = OrgUnit(code="TC01", name="台中通訊處")
    db.add(unit)
    db.commit()
    agent = make_user(db, "agent01", Role.AGENT, unit.id)
    case = svc.create_draft(db, agent)
    q = load_questionnaire()

    _login(page, live_server, "agent01")
    page.goto(f"{live_server}/assessments/{case.case_no}")
    page.wait_for_load_state("networkidle")

    page.fill("#annual_premium", "500萬")
    page.locator("#policy_no").focus()          # 觸發 focusout，立即存檔
    playwright_api.expect(page.locator("#annual_premium_error")).to_be_visible(timeout=8000)
    assert "5000000" in page.locator("#annual_premium_error").inner_text()
    assert "invalid" in (page.locator("#annual_premium").get_attribute("class") or "")

    factor = q.factors[0]
    el = page.locator(f"input[name='{factor.code}'][value='{HIGH_RISK[factor.code]}']")
    el.scroll_into_view_if_needed()
    el.check()
    _expect_text(page.locator("#score-count"), f"1 / {len(q.factors)}",
                 "保費格式錯誤不得波及作答儲存")

    page.fill("#annual_premium", "5000000")
    page.locator("#policy_no").focus()
    playwright_api.expect(page.locator("#annual_premium_error")).to_be_hidden(timeout=8000)
    playwright_api.expect(page.locator("#savehint")).to_contain_text("已儲存", timeout=8000)


def test_rejected_request_reports_reason_and_queue_keeps_running(db, live_server, page):
    """伺服器拒絕的請求：說明原因、丟棄該筆，且不得卡住後續儲存。

    4xx 重試永遠不會成功。舊版一律當成連線問題無限重試，訊息還寫「請確認網路」，
    使用者被引導去檢查網路，真正的原因永遠不會被發現。
    """
    unit = OrgUnit(code="TC01", name="台中通訊處")
    db.add(unit)
    db.commit()
    agent = make_user(db, "agent01", Role.AGENT, unit.id)
    case = svc.create_draft(db, agent)
    q = load_questionnaire()
    first, second = q.factors[0], q.factors[1]

    _login(page, live_server, "agent01")
    page.goto(f"{live_server}/assessments/{case.case_no}")
    page.wait_for_load_state("networkidle")

    # 把第一題的某個選項竄改成不存在的值，模擬伺服器拒絕該筆。
    bad = page.locator(f"input[name='{first.code}']").first
    bad.evaluate("el => el.value = 'not-a-real-option'")
    bad.check()
    playwright_api.expect(page.locator("#savehint")).to_contain_text("未知的選項", timeout=8000)
    assert "網路" not in page.locator("#savehint").inner_text(), "驗證錯誤不得誤導為網路問題"

    el = page.locator(f"input[name='{second.code}'][value='{HIGH_RISK[second.code]}']")
    el.scroll_into_view_if_needed()
    el.check()
    _expect_text(page.locator("#score-count"), f"1 / {len(q.factors)}",
                 "被拒絕的那一筆不得堵住後續作答")
