/*
 * Чат-виджет ИИ-администратора.
 *
 * Плавающая кнопка в углу сайта:
 *   <script src="https://bot.example.ru/widget.js" defer></script>
 *
 * Встроенный в страницу чат:
 *   <div id="chat"></div>
 *   <script src="https://bot.example.ru/widget.js" data-target="#chat" defer></script>
 *
 * Атрибуты: data-api (адрес API, по умолчанию — откуда загружен скрипт),
 * data-target (селектор контейнера), data-open="true" (открыть сразу),
 * data-accent="#2f6df6" и data-accent-ink="#fff" (цвет акцента и текста на нём,
 * можно var(--accent) сайта), data-font="inherit" (шрифт сайта),
 * data-theme="dark|light" (иначе — по html[data-theme] сайта или теме системы).
 */
(function () {
  "use strict";
  if (window.__aiBookingWidget) return;
  window.__aiBookingWidget = true;

  var script = document.currentScript;
  var data = (script && script.dataset) || {};
  var apiBase = (data.api || (script && script.src ? new URL(script.src, location.href).origin : location.origin)).replace(/\/$/, "");
  var inlineTarget = data.target ? document.querySelector(data.target) : null;
  var STORAGE_KEY = "ai-booking-widget:" + apiBase;
  var MAX_STORED = 60;

  var config = null;
  var busy = false;
  var state = loadState() || freshState();

  // ---------- состояние ----------

  function newSessionId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var s = "";
    for (var i = 0; i < 32; i++) s += Math.floor(Math.random() * 16).toString(16);
    return s;
  }

  function freshState() {
    return { sessionId: newSessionId(), messages: [], open: false };
  }

  function loadState() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      return parsed && parsed.sessionId && Array.isArray(parsed.messages) ? parsed : null;
    } catch (e) {
      return null;
    }
  }

  function saveState() {
    try {
      state.messages = state.messages.slice(-MAX_STORED);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {
      /* приватный режим или запрет хранилища — работаем без сохранения */
    }
  }

  // ---------- стили ----------

  var css = [
    ".aibw{--aibw-accent:" + (data.accent || "#2f6df6") + ";--aibw-accent-ink:" + (data.accentInk || "#ffffff") + ";--aibw-bg:#ffffff;--aibw-surface:#f3f5f9;--aibw-text:#1b1f27;--aibw-muted:#667085;--aibw-border:#e3e7ef;--aibw-user:var(--aibw-accent);--aibw-user-text:var(--aibw-accent-ink);--aibw-ok:#12805c;--aibw-ok-bg:#e8f6ef;--aibw-err:#b42318;--aibw-shadow:0 12px 40px rgba(16,24,40,.18);font-size:15px;line-height:1.45;font-family:" + (data.font || "system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif") + ";color:var(--aibw-text);box-sizing:border-box}",
    ".aibw.aibw-dark{--aibw-bg:#13171e;--aibw-surface:#1f2530;--aibw-text:#e8ebf2;--aibw-muted:#98a2b3;--aibw-border:#2a313d;--aibw-ok:#4ad295;--aibw-ok-bg:#132a20;--aibw-err:#ff8a80;--aibw-shadow:0 12px 40px rgba(0,0,0,.5)}",
    ".aibw button,.aibw textarea{font-family:inherit}",
    ".aibw *,.aibw *::before,.aibw *::after{box-sizing:border-box}",
    ".aibw-launcher{position:fixed;right:20px;bottom:20px;width:60px;height:60px;border-radius:50%;border:0;background:var(--aibw-accent);color:var(--aibw-accent-ink);box-shadow:var(--aibw-shadow);cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:2147483000;transition:transform .15s}",
    ".aibw-launcher:hover{transform:scale(1.06)}",
    ".aibw-launcher:focus-visible,.aibw button:focus-visible,.aibw textarea:focus-visible{outline:2px solid var(--aibw-accent);outline-offset:2px}",
    ".aibw-panel{display:flex;flex-direction:column;background:var(--aibw-bg);border:1px solid var(--aibw-border);border-radius:16px;overflow:hidden}",
    ".aibw-floating .aibw-panel{position:fixed;right:20px;bottom:92px;width:380px;height:620px;max-height:calc(100vh - 112px);box-shadow:var(--aibw-shadow);z-index:2147483000}",
    ".aibw-floating .aibw-panel[hidden]{display:none}",
    // Встроенный чат занимает ровно высоту контейнера, а длинный диалог прокручивается внутри.
    // Без height у .aibw-inline панель росла вместе с перепиской и наезжала на блоки ниже.
    ".aibw-inline{height:100%}",
    ".aibw-inline .aibw-panel{width:100%;height:100%;min-height:360px}",
    "@media (max-width:480px){.aibw-floating .aibw-panel{inset:0;width:auto;height:auto;max-height:none;border-radius:0}.aibw-launcher[aria-expanded=true]{display:none}}",
    ".aibw-head{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--aibw-border)}",
    ".aibw-avatar{width:36px;height:36px;border-radius:50%;background:var(--aibw-accent);color:var(--aibw-accent-ink);display:flex;align-items:center;justify-content:center;font-weight:600;flex:none}",
    ".aibw-title{flex:1;min-width:0}",
    ".aibw-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".aibw-sub{font-size:12px;color:var(--aibw-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".aibw-badge{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:6px;background:var(--aibw-surface);color:var(--aibw-muted);font-size:11px;font-weight:500;vertical-align:1px}",
    ".aibw-icon-btn{width:34px;height:34px;border:0;border-radius:8px;background:transparent;color:var(--aibw-muted);cursor:pointer;display:flex;align-items:center;justify-content:center}",
    ".aibw-icon-btn:hover{background:var(--aibw-surface);color:var(--aibw-text)}",
    ".aibw-log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;overscroll-behavior:contain}",
    ".aibw-msg{max-width:86%;padding:9px 12px;border-radius:14px;white-space:pre-wrap;overflow-wrap:anywhere}",
    ".aibw-bot{align-self:flex-start;background:var(--aibw-surface);border-bottom-left-radius:4px}",
    ".aibw-user{align-self:flex-end;background:var(--aibw-user);color:var(--aibw-user-text);border-bottom-right-radius:4px}",
    ".aibw-system{align-self:center;max-width:94%;font-size:13px;color:var(--aibw-err);text-align:center;background:transparent;padding:4px 8px}",
    ".aibw-card{align-self:stretch;border:1px solid var(--aibw-ok);background:var(--aibw-ok-bg);border-radius:12px;padding:10px 12px;font-size:14px}",
    ".aibw-card b{color:var(--aibw-ok)}",
    ".aibw-card a{color:var(--aibw-accent);font-weight:500;text-decoration:none}",
    ".aibw-card a:hover{text-decoration:underline}",
    ".aibw-typing{align-self:flex-start;background:var(--aibw-surface);border-radius:14px;padding:12px 14px;display:flex;gap:4px}",
    ".aibw-typing span{width:6px;height:6px;border-radius:50%;background:var(--aibw-muted);animation:aibw-blink 1.2s infinite}",
    ".aibw-typing span:nth-child(2){animation-delay:.2s}.aibw-typing span:nth-child(3){animation-delay:.4s}",
    "@keyframes aibw-blink{0%,80%,100%{opacity:.25}40%{opacity:1}}",
    "@media (prefers-reduced-motion:reduce){.aibw-typing span{animation:none;opacity:.6}.aibw-launcher{transition:none}}",
    ".aibw-chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 10px}",
    ".aibw-chip{border:1px solid var(--aibw-border);background:var(--aibw-bg);color:var(--aibw-text);border-radius:16px;padding:6px 11px;font:inherit;font-size:13px;cursor:pointer;text-align:left}",
    ".aibw-chip:hover{border-color:var(--aibw-accent);color:var(--aibw-accent)}",
    ".aibw-form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--aibw-border);align-items:flex-end}",
    ".aibw-input{flex:1;resize:none;border:1px solid var(--aibw-border);border-radius:12px;padding:9px 12px;font:inherit;line-height:20px;color:var(--aibw-text);background:var(--aibw-bg);height:40px;max-height:120px;overflow-y:hidden}",
    ".aibw-send{width:40px;height:40px;border:0;border-radius:12px;background:var(--aibw-accent);color:var(--aibw-accent-ink);cursor:pointer;display:flex;align-items:center;justify-content:center;flex:none}",
    ".aibw-send:disabled{opacity:.45;cursor:default}",
    ".aibw-note{padding:0 14px 10px;font-size:11.5px;color:var(--aibw-muted)}",
    // iPhone увеличивает страницу при фокусе на поле со шрифтом меньше 16px
    "@media (pointer:coarse){.aibw-input{font-size:16px}}"
  ].join("\n");

  var ICONS = {
    chat: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>',
    close: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    reset: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>',
    send: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>'
  };

  // ---------- разметка ----------

  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  }

  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var root = el("div", "aibw " + (inlineTarget ? "aibw-inline" : "aibw-floating"));
  var panel = el("section", "aibw-panel");
  panel.setAttribute("aria-label", "Чат с администратором");

  var head = el("div", "aibw-head");
  var avatar = el("div", "aibw-avatar", "AI");
  var title = el("div", "aibw-title");
  var nameEl = el("div", "aibw-name", "Администратор");
  var subEl = el("div", "aibw-sub", "отвечает за несколько секунд");
  title.appendChild(nameEl);
  title.appendChild(subEl);
  var resetBtn = el("button", "aibw-icon-btn", ICONS.reset);
  resetBtn.type = "button";
  resetBtn.title = "Начать заново";
  resetBtn.setAttribute("aria-label", "Начать диалог заново");
  head.appendChild(avatar);
  head.appendChild(title);
  head.appendChild(resetBtn);

  var closeBtn = null;
  if (!inlineTarget) {
    closeBtn = el("button", "aibw-icon-btn", ICONS.close);
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Свернуть чат");
    head.appendChild(closeBtn);
  }

  var log = el("div", "aibw-log");
  log.setAttribute("role", "log");
  log.setAttribute("aria-live", "polite");

  var chips = el("div", "aibw-chips");
  var form = el("form", "aibw-form");
  var input = el("textarea", "aibw-input");
  input.rows = 1;
  input.placeholder = "Напишите сообщение…";
  input.setAttribute("aria-label", "Сообщение");
  var sendBtn = el("button", "aibw-send", ICONS.send);
  sendBtn.type = "submit";
  sendBtn.setAttribute("aria-label", "Отправить");
  form.appendChild(input);
  form.appendChild(sendBtn);
  var note = el("div", "aibw-note");

  panel.appendChild(head);
  panel.appendChild(log);
  panel.appendChild(chips);
  panel.appendChild(form);
  panel.appendChild(note);
  root.appendChild(panel);

  // Тема: явно из data-theme, иначе как у сайта (html[data-theme]), иначе как в системе.
  function detectTheme() {
    if (data.theme === "dark" || data.theme === "light") return data.theme;
    var host = document.documentElement.getAttribute("data-theme");
    if (host === "dark" || host === "light") return host;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function applyTheme() {
    root.classList.toggle("aibw-dark", detectTheme() === "dark");
  }
  applyTheme();
  if (window.MutationObserver) {
    new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }
  if (window.matchMedia) {
    var scheme = window.matchMedia("(prefers-color-scheme: dark)");
    if (scheme.addEventListener) scheme.addEventListener("change", applyTheme);
    else if (scheme.addListener) scheme.addListener(applyTheme);
  }

  var launcher = null;
  if (inlineTarget) {
    inlineTarget.appendChild(root);
  } else {
    launcher = el("button", "aibw-launcher", ICONS.chat);
    launcher.type = "button";
    launcher.setAttribute("aria-label", "Открыть чат с администратором");
    root.appendChild(launcher);
    document.body.appendChild(root);
  }

  // ---------- отрисовка ----------

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function formatPrice(value) {
    return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " ₽";
  }

  function renderMessage(m) {
    if (m.role === "booking") {
      var b = m.booking;
      var html =
        "<b>✓ Запись №" + escapeHtml(b.number) + " создана</b><br>" +
        escapeHtml(b.service) + "<br>" +
        (b.parts ? "Запчасти: " + escapeHtml(b.parts) + "<br>" : "") +
        escapeHtml(b.date_text) + ", " + escapeHtml(b.time) + " · от " + formatPrice(b.total_from || b.price_from);
      if (m.sheetUrl) {
        html += '<br><a href="' + escapeHtml(m.sheetUrl) + '" target="_blank" rel="noopener">Посмотреть в Google Таблице ↗</a>';
      }
      return el("div", "aibw-card", html);
    }
    var cls = m.role === "user" ? "aibw-user" : m.role === "system" ? "aibw-system" : "aibw-bot";
    var node = el("div", "aibw-msg " + cls);
    node.textContent = m.text;
    return node;
  }

  function renderChips() {
    chips.innerHTML = "";
    var userWrote = state.messages.some(function (m) { return m.role === "user"; });
    if (userWrote || !config || !config.quick_replies) return;
    config.quick_replies.forEach(function (text) {
      var chip = el("button", "aibw-chip");
      chip.type = "button";
      chip.textContent = text;
      chip.addEventListener("click", function () { send(text); });
      chips.appendChild(chip);
    });
  }

  function render() {
    log.innerHTML = "";
    state.messages.forEach(function (m) { log.appendChild(renderMessage(m)); });
    if (busy) log.appendChild(el("div", "aibw-typing", "<span></span><span></span><span></span>"));
    renderChips();
    sendBtn.disabled = busy;
    log.scrollTop = log.scrollHeight;
  }

  function push(message) {
    state.messages.push(message);
    saveState();
    render();
  }

  function ensureGreeting() {
    if (config && state.messages.length === 0) {
      state.messages.push({ role: "bot", text: config.greeting });
      saveState();
    }
  }

  // ---------- сеть ----------

  var RESULT_TIMEOUT_MS = 120000;

  function requestJson(url, options) {
    return fetch(url, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось отправить сообщение. Попробуйте ещё раз.");
        return body;
      });
    });
  }

  function waitForResult(jobId, deadline) {
    return new Promise(function (resolve) { setTimeout(resolve, 1000); })
      .then(function () {
        return requestJson(apiBase + "/api/result?job=" + encodeURIComponent(jobId), { cache: "no-store" });
      })
      .then(function (body) {
        if (!body.pending) return body;
        if (Date.now() > deadline) throw new Error("Бот не ответил вовремя. Попробуйте отправить сообщение ещё раз.");
        return waitForResult(jobId, deadline);
      });
  }

  function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    if (config && text.length > config.max_message_chars) {
      push({ role: "system", text: "Сообщение слишком длинное: максимум " + config.max_message_chars + " символов." });
      return;
    }
    input.value = "";
    autosize();
    push({ role: "user", text: text });
    busy = true;
    render();

    requestJson(apiBase + "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, message: text })
    })
      .then(function (body) {
        // Через посредника на сайте ответ готовится асинхронно: забираем его по номеру
        return body.pending ? waitForResult(body.pending, Date.now() + RESULT_TIMEOUT_MS) : body;
      })
      .then(function (body) {
        busy = false;
        state.messages.push({ role: "bot", text: body.reply });
        if (body.booking) {
          state.messages.push({ role: "booking", booking: body.booking, sheetUrl: body.sheet_url });
          window.dispatchEvent(new CustomEvent("ai-booking:created", { detail: body.booking }));
        }
        saveState();
        render();
      })
      .catch(function (err) {
        busy = false;
        var network = !err || err.name === "TypeError";
        var message = network ? "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз." : err.message;
        push({ role: "system", text: message });
      })
      .then(function () { input.focus(); });
  }

  function loadConfig() {
    return fetch(apiBase + "/api/config")
      .then(function (res) { return res.json(); })
      .then(function (cfg) {
        config = cfg;
        nameEl.innerHTML = escapeHtml(cfg.assistant_name) + '<span class="aibw-badge">демо</span>';
        subEl.textContent = cfg.business_name;
        avatar.textContent = (cfg.assistant_name || "AI").charAt(0).toUpperCase();
        note.textContent = cfg.demo_note || "";
        ensureGreeting();
        render();
      })
      .catch(function () {
        push({ role: "system", text: "Чат временно недоступен." });
      });
  }

  // ---------- события ----------

  function autosize() {
    input.style.height = "40px";
    var full = input.scrollHeight + 2; // + рамка
    input.style.height = Math.min(Math.max(full, 40), 120) + "px";
    input.style.overflowY = full > 120 ? "auto" : "hidden";
  }

  function setOpen(open, focus) {
    if (inlineTarget) return;
    state.open = open;
    panel.hidden = !open;
    launcher.innerHTML = open ? ICONS.close : ICONS.chat;
    launcher.setAttribute("aria-label", open ? "Свернуть чат" : "Открыть чат с администратором");
    launcher.setAttribute("aria-expanded", String(open));
    saveState();
    if (open) {
      log.scrollTop = log.scrollHeight;
      if (focus) input.focus();
    }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    send(input.value);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send(input.value);
    }
  });
  input.addEventListener("input", autosize);
  resetBtn.addEventListener("click", function () {
    if (busy) return;
    var open = state.open;
    state = freshState();
    state.open = open;
    ensureGreeting();
    saveState();
    render();
    input.focus();
  });
  if (launcher) {
    launcher.addEventListener("click", function () { setOpen(panel.hidden, true); });
    closeBtn.addEventListener("click", function () { setOpen(false, false); launcher.focus(); });
    panel.hidden = true;
    setOpen(data.open === "true" || state.open === true, false);
  }

  // Для страниц, которые хотят отправить фразу в чат по кнопке.
  window.AIBookingWidget = {
    send: function (text) {
      setOpen(true, false);
      send(text);
    },
    open: function () { setOpen(true, true); }
  };

  render();
  loadConfig();
})();
