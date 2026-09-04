/* 自動儲存：每一次作答立即送出並更新分數。
   離線或伺服器錯誤時進入重試佇列，避免業務員在外勤環境遺失已填資料。 */
(function () {
  "use strict";
  const root = document.getElementById("assessment");
  if (!root) return;

  const caseNo = root.dataset.caseNo;
  const editable = root.dataset.editable === "1";
  const hint = document.getElementById("savehint");
  const pending = [];
  let inflight = false;

  function setHint(text, cls) {
    if (!hint) return;
    hint.textContent = text;
    hint.className = "savehint " + (cls || "");
  }

  async function flush() {
    if (inflight || pending.length === 0) return;
    inflight = true;
    const job = pending[0];
    setHint("儲存中…", "saving");
    try {
      const res = await fetch(job.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(job.body),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      pending.shift();
      if (data && typeof data.total_score === "number") render(data);
      setHint("已儲存 " + new Date().toLocaleTimeString("zh-TW"), "saved");
    } catch (err) {
      setHint("尚未儲存，將自動重試（請確認網路）", "error");
      setTimeout(() => { inflight = false; flush(); }, 4000);
      return;
    }
    inflight = false;
    if (pending.length) flush();
  }

  function queue(url, body) {
    pending.push({ url: url, body: body });
    flush();
  }

  function render(d) {
    const bar = document.getElementById("scorebar");
    const scoreEl = document.getElementById("score-value");
    const levelEl = document.getElementById("score-level");
    const progEl = document.getElementById("score-progress");
    const countEl = document.getElementById("score-count");
    const noticeEl = document.getElementById("score-notice");
    if (scoreEl) scoreEl.textContent = d.total_score;
    if (levelEl) levelEl.textContent = d.level_label;
    if (countEl) countEl.textContent = d.answered + " / " + d.total_factors;
    if (progEl) {
      const span = d.max_score - d.min_score;
      const pct = span > 0 ? Math.max(0, Math.min(100, ((d.total_score - d.min_score) / span) * 100)) : 0;
      progEl.style.width = pct + "%";
    }
    if (bar) {
      bar.classList.toggle("high", d.level === "high");
      bar.classList.toggle("blocked", !!d.blocked);
    }
    if (noticeEl) {
      const msgs = (d.blocked_reasons || []).concat(d.override_reasons || []);
      if (d.blocked) {
        noticeEl.className = "alert danger";
        noticeEl.innerHTML = "<strong>本案應婉拒建立業務關係，請立即通知洗錢防制專責主管。</strong><ul><li>"
          + msgs.map(esc).join("</li><li>") + "</li></ul>";
      } else if (d.level === "high") {
        noticeEl.className = "alert warn";
        noticeEl.innerHTML = "<strong>本案為高風險客戶，送出後須經單位主管同意始得建立業務關係，"
          + "並應瞭解客戶財富及資金來源。</strong>"
          + (msgs.length ? "<ul><li>" + msgs.map(esc).join("</li><li>") + "</li></ul>" : "");
      } else {
        noticeEl.className = "";
        noticeEl.innerHTML = "";
      }
    }
    const submitBtn = document.getElementById("submit-btn");
    if (submitBtn) submitBtn.disabled = !d.complete;
    const missingEl = document.getElementById("missing-hint");
    if (missingEl) {
      missingEl.textContent = d.complete ? "" : "尚有 " + d.missing_factors.length + " 題未填答";
    }
  }

  function esc(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  if (!editable) return;

  root.querySelectorAll(".factor").forEach(function (box) {
    box.addEventListener("change", function (ev) {
      const input = ev.target;
      if (input.type !== "radio") return;
      box.querySelectorAll(".opt").forEach(function (o) { o.classList.remove("selected"); });
      const label = input.closest(".opt");
      if (label) label.classList.add("selected");
      box.classList.add("answered");
      queue("/api/assessments/" + caseNo + "/answer",
            { factor: input.name, option: input.value });
    });
  });

  document.querySelectorAll("[data-check-group]").forEach(function (group) {
    group.addEventListener("change", function () {
      const codes = Array.from(group.querySelectorAll("input:checked")).map(function (i) {
        return i.value;
      });
      queue("/api/assessments/" + caseNo + "/checks",
            { group: group.dataset.checkGroup, codes: codes });
    });
  });

  // 文字欄位在停止輸入 900ms 後才送出，避免每個按鍵都打一次 API。
  const profileBoxes = document.querySelectorAll("[data-autosave-profile]");
  if (profileBoxes.length) {
    let timer = null;
    const saveProfile = function () {
      const body = {};
      profileBoxes.forEach(function (box) {
        box.querySelectorAll("[name]").forEach(function (el) { body[el.name] = el.value; });
      });
      queue("/api/assessments/" + caseNo + "/profile", body);
    };
    profileBoxes.forEach(function (box) {
      box.addEventListener("input", function () {
        clearTimeout(timer);
        setHint("編輯中…", "saving");
        timer = setTimeout(saveProfile, 900);
      });
      // 離開欄位時立即存檔，避免使用者在 debounce 期間關閉頁面。
      box.addEventListener("focusout", function () {
        if (timer) { clearTimeout(timer); timer = null; saveProfile(); }
      });
    });
  }

  // 離開頁面前若仍有未送出的變更，提醒使用者不要關閉。
  window.addEventListener("beforeunload", function (e) {
    if (pending.length) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
})();
