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

  // 業務員的回應中沒有 total_score / level：分數與等級不對第一道防線揭露，
  // 只以「須照會主管」的警示與填答進度呈現。主管以上的回應才帶分數欄位。
  function render(d) {
    const bar = document.getElementById("scorebar");
    const scoreEl = document.getElementById("score-value");
    const levelEl = document.getElementById("score-level");
    const progEl = document.getElementById("score-progress");
    const countEl = document.getElementById("score-count");
    const noticeEl = document.getElementById("score-notice");
    const hasScore = typeof d.total_score === "number";

    if (countEl) countEl.textContent = d.answered + " / " + d.total_factors;
    if (progEl) {
      const pct = d.total_factors ? (d.answered / d.total_factors) * 100 : 0;
      progEl.style.width = Math.max(0, Math.min(100, pct)) + "%";
    }
    if (hasScore) {
      if (scoreEl) scoreEl.textContent = d.total_score;
      if (levelEl) levelEl.textContent = d.level_label;
    }
    if (bar) {
      bar.classList.toggle("high", hasScore ? d.level === "high" : !!d.needs_consultation);
      bar.classList.toggle("blocked", !!d.blocked);
    }
    if (noticeEl) {
      if (d.blocked) {
        noticeEl.className = "alert danger";
        noticeEl.innerHTML =
          "<strong>本案應婉拒建立業務關係，請停止招攬並立即通知洗錢防制專責主管。</strong>"
          + "<ul><li>" + (d.blocked_reasons || []).map(esc).join("</li><li>") + "</li></ul>";
      } else if (d.needs_consultation) {
        noticeEl.className = "alert warn";
        noticeEl.innerHTML =
          "<strong>本案須照會單位主管確認後，方得建立業務關係。</strong>"
          + "請於下方「照會主管確認」區塊完成登錄，並填列客戶財富來源與資金之實質來源。";
      } else {
        noticeEl.className = "";
        noticeEl.innerHTML = "";
      }
    }

    // 照會區塊只在需要時出現；已完成照會則改為顯示紀錄。
    const consultBlock = document.getElementById("consult-block");
    if (consultBlock) consultBlock.style.display = d.needs_consultation ? "" : "none";
    const consultForm = document.getElementById("consult-form");
    const consultDone = document.getElementById("consult-done");
    if (consultForm) consultForm.hidden = !!d.consulted;
    if (consultDone) {
      consultDone.hidden = !d.consulted;
      if (d.consulted) {
        consultDone.className = "alert info";
        consultDone.innerHTML = "已照會 <strong>" + esc(d.consulted_name || "") + "</strong>";
      }
    }

    const blocking = !d.complete || (d.needs_consultation && !d.consulted);
    const submitBtn = document.getElementById("submit-btn");
    if (submitBtn) submitBtn.disabled = blocking;
    const missingEl = document.getElementById("missing-hint");
    if (missingEl) {
      missingEl.textContent = !d.complete
        ? "尚有 " + d.missing_factors.length + " 題未填答"
        : (d.needs_consultation && !d.consulted ? "請先完成上方「照會主管確認」" : "");
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

  const consultBtn = document.getElementById("consult-btn");
  if (consultBtn) {
    consultBtn.addEventListener("click", async function () {
      const input = document.getElementById("supervisor_name");
      const errEl = document.getElementById("consult-error");
      const name = (input.value || "").trim();
      if (!name) {
        errEl.textContent = "請填寫照會之主管姓名";
        input.focus();
        return;
      }
      errEl.textContent = "";
      consultBtn.disabled = true;
      try {
        const res = await fetch("/api/assessments/" + caseNo + "/consult", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ supervisor_name: name }),
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        render(await res.json());
        setHint("已登錄照會紀錄", "saved");
      } catch (err) {
        errEl.textContent = "登錄失敗，請確認網路後再試";
        consultBtn.disabled = false;
      }
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
