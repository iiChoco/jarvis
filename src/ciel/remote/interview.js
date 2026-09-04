// The interview room's behaviour — everything the page does, in one file,
// touching the DOM only through the ids, data attributes, and <template>s
// in interview.html (the contract).
//
// Views: sign in, lobby, setup, brief, live (socket, speech, playback,
// recording, the code editor), review, admin. The hub owns every decision
// about whose turn it is; this side reports what the microphone hears,
// plays what the interviewer says, records the mix, and mirrors state.
(() => {
  "use strict";

  const API = "/interview/api";
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const body = document.body;
  const text = (sel, value) => { const el = $(sel); if (el) el.textContent = value == null ? "" : String(value); };

  let me = null;      // {username, role, caps, limits}
  let current = null; // {session, setup, brief, transcript, code, debrief, debrief_md}

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function shortDate(iso) {
    if (!iso) return "";
    const d = new Date(iso.replace(" ", "T"));
    if (isNaN(d)) return iso.slice(0, 10);
    return `${String(d.getDate()).padStart(2, "0")} ${MONTHS[d.getMonth()]}`;
  }
  function fmtDuration(s) {
    if (s == null || isNaN(s)) return "";
    const m = Math.floor(s / 60), sec = Math.floor(s % 60);
    return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }
  function modeLabel(session) {
    if (!session) return "";
    if (session.mode === "case") {
      const src = (current && current.setup && current.setup.case_source) || (session.setup && session.setup.case_source);
      return src === "generated" ? "case · gen" : "case · hist";
    }
    return session.mode || "";
  }

  // ── views ──────────────────────────────────────────────────────────────

  function show(view) {
    body.dataset.view = view;
    $("#nav-lobby").hidden = !me || view === "lobby" || view === "live";
    $("#nav-admin").hidden = !me || me.role !== "admin" || view === "admin" || view === "live";
    $("#nav-logout").hidden = !me || view === "live";
    $("#nav-password").hidden = !me || view === "live";
    text("#whoami", me ? (me.role === "admin" ? `${me.username} · owner` : me.username) : "");
    text("#brand-sub", view === "admin" ? "INTERVIEW · ADMIN" : "INTERVIEW");
  }

  function flag(el, on) { if (el) el.dataset.show = on ? "1" : "0"; }

  // ── fetch ──────────────────────────────────────────────────────────────

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(API + path, init);
    if (res.status === 401) {
      me = null;
      show("login");
      throw new Error("sign in");
    }
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) throw new Error((data && data.reason) || res.statusText || "request failed");
    return data;
  }

  // ── login ──────────────────────────────────────────────────────────────

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#login-error");
    flag(err, false);
    try {
      me = await api("/login", {
        method: "POST",
        body: { username: $("#login-user").value.trim().toLowerCase(), password: $("#login-pass").value },
      });
      $("#login-pass").value = "";
      applyCaps();
      enterLobby();
    } catch (ex) {
      err.textContent = ex.message;
      flag(err, true);
    }
  });

  $("#nav-logout").addEventListener("click", async () => {
    try { await api("/logout", { method: "POST" }); } catch (_) {}
    me = null;
    show("login");
  });
  $("#nav-lobby").addEventListener("click", enterLobby);
  $("#nav-admin").addEventListener("click", enterAdmin);

  // ── change my password ─────────────────────────────────────────────────

  $("#nav-password").addEventListener("click", () => {
    $("#password-current").value = ""; $("#password-new").value = "";
    flag($("#password-error"), false);
    flag($("#password-dialog"), true);
    $("#password-current").focus();
  });
  $("#password-cancel").addEventListener("click", () => flag($("#password-dialog"), false));
  $("#password-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    flag($("#password-error"), false);
    try {
      await api("/password", { method: "POST", body: { current: $("#password-current").value, new: $("#password-new").value } });
      flag($("#password-dialog"), false);
    } catch (ex) {
      text("#password-error", ex.message);
      flag($("#password-error"), true);
    }
  });

  const hasSTT = () => "SpeechRecognition" in window || "webkitSpeechRecognition" in window;
  const hasAudio = () => "AudioContext" in window || "webkitAudioContext" in window;

  function applyCaps() {
    const caps = (me && me.caps) || {};
    body.dataset.tts = caps.tts || "browser";
    body.dataset.stt = hasSTT() ? "browser" : "none";
  }

  // ── lobby ──────────────────────────────────────────────────────────────

  async function enterLobby() {
    show("lobby");
    const list = $("#sessions");
    list.replaceChildren();
    let sessions = [];
    try { sessions = (await api("/sessions")).sessions || []; } catch (_) { sessions = []; }
    const name = me ? me.username.charAt(0).toUpperCase() + me.username.slice(1) : "";
    const ready = sessions.filter((s) => s.debriefed);
    if (!sessions.length) {
      text("#lobby-greeting", `Hello, ${name}. Nothing here yet — your first interview will be. Here's how it goes.`);
    } else {
      const latest = ready[0];
      text("#lobby-greeting", `Welcome back, ${name}. ${sessions.length} session${sessions.length === 1 ? "" : "s"} so far` +
        (latest ? ` — your ${latest.title.split(" · ")[0]} debrief from ${shortDate(latest.created)} is ready.` : "."));
    }
    flag($("#sessions-empty"), sessions.length === 0);
    $("#lobby-list").dataset.empty = sessions.length ? "0" : "1";
    text("#sessions-count", sessions.length || "");
    const tpl = $("#session-row");
    for (const s of sessions) {
      const row = tpl.content.firstElementChild.cloneNode(true);
      row.dataset.id = s.id;
      row.dataset.mode = s.mode || "";
      row.dataset.state = s.debriefed ? "debriefed" : (s.state || "");
      row.querySelector("[data-field=date]").textContent = shortDate(s.created);
      row.querySelector("[data-field=title]").textContent = s.title || "(untitled)";
      row.querySelector("[data-field=mode]").textContent = s.mode === "case" ? "case" : (s.mode || "");
      row.querySelector("[data-field=duration]").textContent = s.duration_s ? fmtDuration(s.duration_s) : "—";
      row.querySelector("[data-field=debrief]").textContent =
        s.debriefed ? "ready" : s.state === "prepared" ? "not started" : s.state === "live" ? "in progress" : s.state === "ended" ? "writing…" : (s.state || "");
      row.addEventListener("click", () => openSession(s));
      row.querySelector("[data-action=delete]").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("Delete this interview and its recording?")) return;
        try { await api(`/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" }); } catch (ex) { alert(ex.message); }
        enterLobby();
      });
      list.appendChild(row);
    }
  }

  $("#new-interview").addEventListener("click", () => show("setup"));

  async function openSession(s) {
    try { current = await api(`/sessions/${encodeURIComponent(s.id)}`); } catch (_) { return; }
    const state = current.session.state;
    if (state === "prepared") renderBrief();
    else if (state === "live") enterLive();
    else enterReview();
  }

  // ── setup ──────────────────────────────────────────────────────────────

  for (const radio of $$("#setup-form [name=mode]")) {
    radio.addEventListener("change", () => { body.dataset.setupMode = radio.value; });
  }
  const checked = (name) => { const el = $(`#setup-form [name=${name}]:checked`); return el ? el.value : ""; };

  $("#setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const mode = checked("mode") || "company";
    const setup = {
      mode,
      request: mode === "case" ? $("#setup-request-case").value : $("#setup-request").value,
      role: $("#setup-role").value,
      seniority: checked("seniority"),
      focus: $("#setup-focus").value,
      length_min: Number(checked("length") || 30),
      case_type: checked("case_type") || "any",
      case_source: checked("case_source") || "library",
    };
    $("#setup-submit").disabled = true;
    flag($("#setup-busy"), true);
    try {
      const r = await api("/sessions", { method: "POST", body: setup });
      current = { session: r.session, setup, brief: r.brief, transcript: [], code: [] };
      renderBrief();
    } catch (ex) {
      alert(ex.message);
    } finally {
      $("#setup-submit").disabled = false;
      flag($("#setup-busy"), false);
    }
  });

  // ── brief ──────────────────────────────────────────────────────────────

  function setField(name, value) {
    for (const el of $$(`section[data-view=brief] [data-field="${name}"]`)) {
      if (Array.isArray(value)) {
        el.replaceChildren(...value.filter(Boolean).map((v) => { const s = document.createElement("span"); s.textContent = String(v); return s; }));
      } else {
        el.textContent = value == null ? "" : String(value);
      }
    }
  }
  const joinBits = (...bits) => bits.filter(Boolean).join(" · ");

  function renderBrief() {
    show("brief");
    const b = current.brief || {};
    const mode = current.session.mode;
    for (const el of $$("section[data-view=brief] [data-field]")) el.replaceChildren();
    $("#brief-questions").replaceChildren();
    $("#brief-exhibits").replaceChildren();
    text("#brief-mode", mode === "company" ? "" : modeLabel(current.session) + (mode === "case" && b.type ? ` · ${b.type}` : ""));
    text("#brief-regenerate-label", mode === "case" ? "another case" : "regenerate");
    if (mode === "case") {
      text("#brief-kicker", "the prompt · as the interviewer will read it");
      setField("name", b.title);
      setField("case_prompt", b.prompt);
      setField("case_client", b.client ? `${b.client}${b.year ? `, around ${b.year}` : ""} — a stand-in name.` : "");
      setField("case_type", b.type);
      setField("case_expects", "Clarifying questions, a structure said out loud, a request for data before an opinion, arithmetic done in the open, and a recommendation with the risk you'd watch.");
      setField("case_outcome", "Sealed. What the real client did, and how it went, is shown next to your recommendation in the review — not before.");
      text("#brief-right-kicker", "data that may be shared");
      const ex = b.exhibits || [];
      text("#brief-right-count", ex.length ? `${ex.length} · only when you ask for the right thing` : "");
      for (const e of ex) {
        const row = document.createElement("div");
        const n = document.createElement("span"); n.className = "n"; n.textContent = String(e.id || "").toUpperCase();
        const t = document.createElement("span"); t.textContent = e.title || "";
        const k = document.createElement("span"); k.className = "k"; k.textContent = e.kind || "table";
        row.append(n, t, k);
        $("#brief-exhibits").appendChild(row);
      }
    } else {
      const c = b.company || {}, r = b.role || {}, who = b.interviewer || {};
      text("#brief-kicker", mode === "technical" ? "the company · fictional · technical round" : "the company · fictional");
      setField("name", c.name);
      setField("tagline", c.tagline);
      setField("business_model", c.business_model);
      setField("industry", c.industry);
      setField("stage_size", joinBits(c.stage, c.size));
      setField("hq_founded", joinBits(c.hq, c.founded ? `founded ${c.founded}` : ""));
      setField("products", c.products || []);
      setField("recent_news", c.recent_news || []);
      setField("culture", c.culture);
      setField("role_title", joinBits(r.title, r.level));
      setField("role_team", r.team);
      setField("role_responsibilities", r.responsibilities || []);
      setField("role_must_haves", r.must_haves || []);
      setField("interviewer", who.name ? `${who.name}, ${who.title}. ${who.style || ""}` : "");
      if (mode === "technical" && b.technical) {
        setField("tech_focus", b.technical.focus);
        setField("tech_expect", b.technical.what_to_expect || []);
      }
      const qs = b.likely_questions || [];
      text("#brief-right-kicker", "likely questions");
      text("#brief-right-count", qs.length ? `${qs.length} · not all will be asked` : "");
      const tpl = $("#question-row");
      qs.forEach((q, i) => {
        const row = tpl.content.firstElementChild.cloneNode(true);
        row.querySelector(".n").textContent = String(i + 1).padStart(2, "0");
        row.querySelector("[data-field=q]").textContent = q.q || "";
        row.querySelector("[data-field=kind]").textContent = q.kind || "";
        $("#brief-questions").appendChild(row);
      });
    }
    $("#brief-regenerate").disabled = false;
  }

  $("#brief-regenerate").addEventListener("click", async () => {
    if (!current) return;
    $("#brief-regenerate").disabled = true;
    text("#brief-regenerate-label", "writing…");
    try {
      const r = await api(`/sessions/${encodeURIComponent(current.session.id)}/regenerate`, { method: "POST" });
      current.session = r.session; current.brief = r.brief;
      renderBrief();
    } catch (ex) {
      alert(ex.message);
      $("#brief-regenerate").disabled = false;
      renderBrief();
    }
  });

  $("#brief-begin").addEventListener("click", () => enterLive());

  // ── live ───────────────────────────────────────────────────────────────

  const live = {
    ws: null, ended: false, closing: false, turn: 0, state: "idle",
    audio: null, mix: null, queue: [], playing: false, turnEnded: false, currentSource: null,
    recorder: null, recSeq: 0, recognition: null, muted: false, flushResolve: null,
    partialAt: 0, timer: null, elapsedBase: 0, elapsedFrom: 0, questions: 0, length: 30,
    problem: null, starterShown: "", snapshotTimer: null, cm: null, retry: null,
    debriefReady: false, holdAnim: null, exhibitCount: 0, holdWasListening: false,
  };

  async function enterLive() {
    if (!current) return;
    show("live");
    body.dataset.state = "idle";
    body.dataset.rec = "off";
    Object.assign(live, { ended: false, closing: false, turn: 0, questions: 0, queue: [], playing: false, debriefReady: false,
                          problem: null, exhibitCount: 0, muted: false, turnEnded: false, holdWasListening: false });
    const s = current.session, b = current.brief || {};
    live.length = Number((current.setup && current.setup.length_min) || (s.setup && s.setup.length_min) || 30);
    text("#q-text", ""); $("#q-text").dataset.dim = "0";
    text("#partial", "");
    $("#transcript").replaceChildren();
    $("#exhibits").replaceChildren();
    $("#problem").hidden = true;
    $("#editor").hidden = true;
    $("#live-grid").dataset.side = "0";
    $("#live-grid").dataset.editor = "0";
    flag($("#apology"), false);
    text("#q-count", "Q 0");
    text("#q-number", "question 01");
    text("#timer", "00:00");
    text("#timer-total", `/ ${live.length}`);
    text("#btn-mute", "MIC · ON");
    text("#ring-label", "waiting");
    text("#live-hint", "hands free — I wait two seconds of silence");
    text("#mode-chip", s.mode === "company" ? "" : modeLabel(s));
    const who = b.interviewer || {};
    text("#q-caption", who.name ? `${who.name} · ${who.title || "interviewer"}` : "interviewer");
    text("#side-title", s.mode === "technical" ? "editor" : "exhibits");
    text("#side-count", "");
    text("#side-foot", s.mode === "technical" ? "talk while you type · submit when you're ready" : "stays open while you talk · click a header to swap");
    await startRecording();   // asks for the microphone; a refusal means no recording, not no interview
    connectLive();
  }

  function connectLive() {
    if (live.retry) { clearTimeout(live.retry); live.retry = null; }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/interview/ws?session=${encodeURIComponent(current.session.id)}`);
    ws.binaryType = "arraybuffer";
    live.ws = ws;
    ws.onopen = () => {
      body.dataset.conn = "ok";
      ws.send(JSON.stringify({
        type: "hello", v: 1,
        caps: { stt: hasSTT() ? "browser" : "none", tts: hasAudio() ? "browser" : "none", record: !!live.recorder },
      }));
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") return;
      let frame;
      try { frame = JSON.parse(e.data); } catch (_) { return; }
      onFrame(frame);
    };
    ws.onclose = (e) => {
      if (live.ws !== ws) return;
      live.ws = null;
      if (live.ended || live.closing) return;
      body.dataset.conn = "lost";
      if (e.code === 1008 || e.code === 4401 || e.code === 4400) return;
      live.retry = setTimeout(connectLive, 2000);
    };
    ws.onerror = () => {};
  }

  function send(frame) {
    if (live.ws && live.ws.readyState === WebSocket.OPEN) live.ws.send(JSON.stringify(frame));
  }

  function onFrame(f) {
    switch (f.type) {
      case "hello":
        body.dataset.tts = f.tts;
        live.elapsedBase = f.elapsed_s; live.elapsedFrom = performance.now();
        startTimer();
        for (const h of f.history || []) {
          if (h.type === "say") { if (h.turn !== live.turn) { live.turn = h.turn; text("#q-text", ""); } appendQuestion(h.text); }
          else if (h.type === "exhibit") renderExhibit(h);
          else if (h.type === "problem") renderProblem(h);
          else if (h.type === "transcript") addTranscript($("#transcript"), h);
        }
        live.turn = f.turn;
        setQuestionNumber(f.turn);
        setState(f.state, 0);
        break;
      case "state":
        setState(f.state, f.hold_ms || 0);
        break;
      case "say":
        if (f.turn !== live.turn || f.n === 1) {
          live.turn = f.turn; text("#q-text", ""); live.turnEnded = false;
          $("#q-text").dataset.dim = "0"; flag($("#apology"), false);
          setQuestionNumber(f.turn);
        }
        appendQuestion(f.text);
        enqueueSpeech(f);
        break;
      case "turn.end":
        live.turnEnded = true;
        live.questions = Math.max(0, f.turn - 1);
        text("#q-count", `Q ${live.questions}`);
        maybeReportPlayed();
        break;
      case "exhibit": renderExhibit(f); break;
      case "problem": renderProblem(f); break;
      case "apology":
        stopPlayback();
        $("#q-text").dataset.dim = "1";
        text("#apology-cap", `ciel · spoke over you · ${fmtDuration(nowS())}`);
        text("#apology-text", f.text);
        flag($("#apology"), true);
        break;
      case "transcript":
        addTranscript($("#transcript"), f);
        break;
      case "debrief.ready":
        live.debriefReady = true;
        if (live.ended) finishLive();
        break;
      case "error":
        if (f.code === "room_full") { alert(f.reason); live.closing = true; enterLobby(); }
        else if (f.code === "debrief") { if (live.ended) finishLive(); }
        else console.warn("room error", f);
        break;
      case "pong": break;
    }
  }

  function setQuestionNumber(turn) {
    text("#q-number", `question ${String(Math.max(1, turn)).padStart(2, "0")}`);
  }

  function setState(state, holdMs) {
    live.state = state;
    body.dataset.state = state;
    text("#state", state);
    if (state === "listening") {
      text("#ring-label", holdMs && live.holdWasListening ? "still listening" : "listening");
      live.holdWasListening = true;
      animateHold(holdMs);
      startRecognition();
    } else {
      live.holdWasListening = false;
      stopHold();
      text("#ring-label", state === "thinking" ? "thinking" : state === "speaking" ? "ciel is speaking" : state === "ended" ? "over" : "waiting");
    }
    if (state === "thinking") text("#partial", "");
    if (state === "ended") {
      live.ended = true;
      stopRecognition();
      text("#live-hint", "writing your debrief…");
      if (live.debriefReady) finishLive();
      else setTimeout(() => { if (live.ended && body.dataset.view === "live") finishLive(); }, 90000);
    }
  }

  function appendQuestion(t) {
    const el = $("#q-text");
    el.textContent = (el.textContent ? el.textContent + " " : "") + t;
  }

  function addTranscript(container, row) {
    const tpl = $("#transcript-row");
    const el = tpl.content.firstElementChild.cloneNode(true);
    el.dataset.speaker = row.speaker;
    el.dataset.t = row.t;
    el.querySelector("[data-field=t]").textContent = fmtDuration(row.t);
    el.querySelector("[data-field=text]").textContent = row.text;
    container.appendChild(el);
    return el;
  }

  function renderExhibit(f) {
    const tpl = $("#exhibit-card");
    const card = tpl.content.firstElementChild.cloneNode(true);
    card.dataset.id = f.id;
    live.exhibitCount += 1;
    card.querySelector(".k").textContent = `exhibit ${live.exhibitCount} · shared ${fmtDuration(nowS())}`;
    card.querySelector("[data-field=title]").textContent = f.title;
    card.querySelector("[data-field=note]").textContent = f.note || "";
    const table = card.querySelector("table");
    if (f.kind === "table" && table) {
      const thead = document.createElement("thead"), tr = document.createElement("tr");
      for (const c of f.columns || []) { const th = document.createElement("th"); th.textContent = c; tr.appendChild(th); }
      thead.appendChild(tr); table.appendChild(thead);
      const tbody = document.createElement("tbody");
      for (const r of f.rows || []) {
        const row = document.createElement("tr");
        for (const c of r) { const td = document.createElement("td"); td.textContent = c; row.appendChild(td); }
        tbody.appendChild(row);
      }
      table.appendChild(tbody);
    } else if (table) {
      table.remove();
    }
    for (const other of $$("#exhibits .card")) collapse(other, true);
    card.querySelector(".head").addEventListener("click", () => {
      const open = !card.classList.contains("collapsed");
      for (const other of $$("#exhibits .card")) collapse(other, true);
      collapse(card, open);
    });
    $("#exhibits").prepend(card);
    $("#live-grid").dataset.side = "1";
    text("#side-count", live.exhibitCount);
  }

  function collapse(card, yes) {
    card.classList.toggle("collapsed", yes);
    card.querySelector(".open").textContent = yes ? "▾ open" : "";
  }

  // ── timer & hold ring ──────────────────────────────────────────────────

  function startTimer() {
    if (live.timer) clearInterval(live.timer);
    live.timer = setInterval(() => text("#timer", fmtDuration(nowS())), 500);
  }

  function animateHold(ms) {
    stopHold();
    const ring = $("#hold-ring");
    if (!ring || !ms) return;
    const start = performance.now();
    const step = () => {
      const p = Math.min(1, (performance.now() - start) / ms);
      ring.style.setProperty("--hold", String(p));
      if (p < 1 && live.state === "listening") live.holdAnim = requestAnimationFrame(step);
    };
    live.holdAnim = requestAnimationFrame(step);
  }

  function stopHold() {
    if (live.holdAnim) cancelAnimationFrame(live.holdAnim);
    live.holdAnim = null;
    const ring = $("#hold-ring");
    if (ring) ring.style.setProperty("--hold", "0");
  }

  // ── the interviewer's voice ────────────────────────────────────────────

  function audioContext() {
    if (!live.audio && hasAudio()) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      live.audio = new Ctx();
    }
    if (live.audio && live.audio.state === "suspended") live.audio.resume();
    return live.audio;
  }

  function enqueueSpeech(f) {
    live.queue.push(f);
    if (!live.playing) playNext();
  }

  async function playNext() {
    const f = live.queue.shift();
    if (!f) {
      live.playing = false;
      maybeReportPlayed();
      return;
    }
    live.playing = true;
    stopRecognition(); // the mic would hear the interviewer
    try {
      if (f.audio && audioContext()) {
        const bytes = Uint8Array.from(atob(f.audio), (c) => c.charCodeAt(0));
        const buf = await live.audio.decodeAudioData(bytes.buffer);
        await new Promise((resolve) => {
          const src = live.audio.createBufferSource();
          src.buffer = buf;
          src.connect(live.audio.destination);
          if (live.mix) src.connect(live.mix);
          src.onended = resolve;
          live.currentSource = src;
          src.start();
        });
        live.currentSource = null;
      } else if ("speechSynthesis" in window) {
        await new Promise((resolve) => {
          const u = new SpeechSynthesisUtterance(f.text);
          u.onend = resolve; u.onerror = resolve;
          speechSynthesis.speak(u);
        });
      }
    } catch (ex) {
      console.warn("playback failed", ex);
    }
    playNext();
  }

  function stopPlayback() {
    live.queue = [];
    if (live.currentSource) { try { live.currentSource.stop(); } catch (_) {} live.currentSource = null; }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
  }

  function maybeReportPlayed() {
    if (live.turnEnded && !live.playing && live.queue.length === 0) {
      live.turnEnded = false;
      send({ type: "played", turn: live.turn });
    }
  }

  // ── the candidate's voice ──────────────────────────────────────────────

  function startRecognition() {
    if (!hasSTT() || live.muted || live.ended || live.playing) return;
    if (live.recognition) return;
    const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
    const rec = new Rec();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = navigator.language || "en-US";
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        const t = r[0].transcript.trim();
        if (!t) continue;
        if (r.isFinal) send({ type: "speech.final", text: t, t: nowS() });
        else interim += (interim ? " " : "") + t;
      }
      if (live.state === "thinking" || live.state === "speaking") {
        stopPlayback();
        send({ type: "barge", t: nowS() });
      }
      text("#partial", interim);
      const now = performance.now();
      if (interim && now - live.partialAt > 250) {
        live.partialAt = now;
        send({ type: "speech.partial", text: interim });
      }
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        body.dataset.stt = "none";
        live.recognition = null;
      }
    };
    rec.onend = () => {
      live.recognition = null;
      if (!live.ended && !live.muted && !live.playing && body.dataset.view === "live") setTimeout(startRecognition, 150);
    };
    try { rec.start(); live.recognition = rec; } catch (_) { live.recognition = null; }
  }

  function stopRecognition() {
    const rec = live.recognition;
    live.recognition = null;
    if (rec) { rec.onend = null; try { rec.stop(); } catch (_) {} }
  }

  function nowS() { return live.elapsedBase + (performance.now() - live.elapsedFrom) / 1000; }

  $("#btn-mute").addEventListener("click", () => {
    live.muted = !live.muted;
    text("#btn-mute", live.muted ? "MIC · OFF" : "MIC · ON");
    if (live.muted) stopRecognition(); else startRecognition();
  });

  $("#btn-done").addEventListener("click", () => { stopPlayback(); send({ type: "answer.done" }); });

  $("#typed-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const t = $("#typed-text").value.trim();
    if (!t) return;
    stopPlayback();
    send({ type: "typed", text: t });
    $("#typed-text").value = "";
  });

  $("#btn-end").addEventListener("click", () => {
    if (!confirm("End the interview?")) return;
    stopRecognition();
    stopPlayback();
    send({ type: "end" });
  });

  // ── recording ──────────────────────────────────────────────────────────

  async function startRecording() {
    live.recorder = null; live.recSeq = 0; live.mix = null;
    if (!navigator.mediaDevices || !("MediaRecorder" in window) || !hasAudio()) return;
    try {
      const mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      const ctx = audioContext();
      live.mix = ctx.createMediaStreamDestination();
      ctx.createMediaStreamSource(mic).connect(live.mix);
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus"
        : MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "";
      const rec = new MediaRecorder(live.mix.stream, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : undefined);
      rec.ondataavailable = async (e) => {
        if (!e.data || !e.data.size) return;
        const buf = await e.data.arrayBuffer();
        const out = new Uint8Array(4 + buf.byteLength);
        new DataView(out.buffer).setUint32(0, live.recSeq++);
        out.set(new Uint8Array(buf), 4);
        if (live.ws && live.ws.readyState === WebSocket.OPEN) live.ws.send(out.buffer);
        if (live.flushResolve && rec.state === "inactive") live.flushResolve();
      };
      rec.start(5000);
      live.recorder = rec;
      body.dataset.rec = "on";
    } catch (ex) {
      console.warn("no recording", ex);
    }
  }

  async function stopRecording() {
    const rec = live.recorder;
    live.recorder = null;
    body.dataset.rec = "off";
    if (!rec || rec.state === "inactive") return;
    await new Promise((resolve) => {
      live.flushResolve = resolve;
      setTimeout(resolve, 1500);
      rec.stop();
    });
    live.flushResolve = null;
    for (const t of rec.stream.getTracks()) t.stop();
  }

  async function finishLive() {
    if (body.dataset.view !== "live") return;
    live.closing = true;
    if (live.timer) clearInterval(live.timer);
    stopRecognition();
    stopPlayback();
    await stopRecording();
    if (live.ws) { try { live.ws.close(); } catch (_) {} live.ws = null; }
    try { current = await api(`/sessions/${encodeURIComponent(current.session.id)}`); } catch (_) {}
    enterReview();
  }

  // ── the code editor (technical mode) ───────────────────────────────────

  function editorValue() { return live.cm ? live.cm.getValue() : $("#code-editor").value; }
  function setEditor(t) { if (live.cm) live.cm.setValue(t); else $("#code-editor").value = t; }

  function renderProblem(f) {
    live.problem = f;
    const p = $("#problem");
    p.hidden = false;
    $("#editor").hidden = false;
    $("#live-grid").dataset.side = "1";
    $("#live-grid").dataset.editor = "1";
    p.querySelector("[data-field=title]").textContent = f.title;
    p.querySelector("[data-field=meta]").textContent = [f.difficulty, f.topic].filter(Boolean).join(" · ");
    p.querySelector("[data-field=statement]").textContent = f.statement;
    const ex = p.querySelector("[data-field=examples]"); ex.replaceChildren();
    for (const e of f.examples || []) {
      const li = document.createElement("li");
      li.textContent = `${e.input} → ${e.output}${e.note ? `  (${e.note})` : ""}`;
      ex.appendChild(li);
    }
    const cs = p.querySelector("[data-field=constraints]"); cs.replaceChildren();
    for (const c of f.constraints || []) { const li = document.createElement("li"); li.textContent = c; cs.appendChild(li); }
    upgradeEditor();
    applyStarter();
  }

  function upgradeEditor() {
    if (live.cm || !window.CodeMirror) return;
    try {
      live.cm = CodeMirror.fromTextArea($("#code-editor"), { lineNumbers: true, indentUnit: 4, tabSize: 4 });
      live.cm.on("change", scheduleSnapshot);
    } catch (_) { live.cm = null; }
  }

  const CM_MODES = { python: "python", c: "text/x-csrc", cpp: "text/x-c++src", java: "text/x-java", rust: "rust",
                     javascript: "javascript", typescript: "text/typescript", go: "go" };

  function applyStarter() {
    const lang = $("#code-lang").value;
    const starter = (live.problem && live.problem.starter && live.problem.starter[lang]) || "";
    const cur = editorValue();
    if (!cur.trim() || cur === live.starterShown) setEditor(starter);
    live.starterShown = starter;
    if (live.cm) live.cm.setOption("mode", CM_MODES[lang] || null);
  }

  $("#code-lang").addEventListener("change", applyStarter);
  $("#code-editor").addEventListener("input", scheduleSnapshot);
  $("#code-editor").addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !live.cm) {
      e.preventDefault();
      const ta = e.target, s = ta.selectionStart, end = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + "    " + ta.value.slice(end);
      ta.selectionStart = ta.selectionEnd = s + 4;
      scheduleSnapshot();
    }
  });

  function scheduleSnapshot() {
    if (live.snapshotTimer) return;
    live.snapshotTimer = setTimeout(() => {
      live.snapshotTimer = null;
      const code = editorValue();
      if (code.trim() && code !== live.starterShown) send({ type: "code.snapshot", lang: $("#code-lang").value, code });
    }, 10000);
  }

  $("#btn-submit-code").addEventListener("click", () => {
    const code = editorValue();
    if (!code.trim()) return;
    if (live.snapshotTimer) { clearTimeout(live.snapshotTimer); live.snapshotTimer = null; }
    send({ type: "code.submit", lang: $("#code-lang").value, code });
  });

  // ── review ─────────────────────────────────────────────────────────────

  const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

  function enterReview() {
    show("review");
    const s = current.session, d = current.debrief, b = current.brief || {};
    text("section[data-view=review] [data-field=title]", s.title || "");
    text("section[data-view=review] [data-field=meta]", `${shortDate(s.created)} · ${fmtDuration(s.duration_s) || "—"} · ${s.question_count || 0} q`);
    text("#debrief-kicker", d ? `debrief · written ${shortDate(s.ended || s.created)}` : "debrief");

    // the transcript, with click-to-seek
    const player = $("#player");
    const tr = $("#review-transcript");
    tr.replaceChildren();
    const rows = current.transcript || [];
    for (const row of rows) {
      const line = addTranscript(tr, row);
      line.addEventListener("click", () => { if (s.has_recording) { player.currentTime = row.t; player.play(); } });
    }

    // the player
    const ui = $("#player-ui");
    if (s.has_recording) {
      ui.hidden = false;
      player.src = `${API}/sessions/${encodeURIComponent(s.id)}/recording`;
      setupPlayer(player, rows, s.duration_s || 0);
    } else {
      ui.hidden = true;
      player.removeAttribute("src");
    }

    // the outcome (case)
    const outcome = $("#review-outcome");
    outcome.replaceChildren();
    if (s.mode === "case" && b.historical_outcome) {
      const left = el("div", "col");
      const said = d && d.case && d.case.recommendation_given && d.case.recommendation_given !== "n/a";
      left.append(el("span", "k", "you recommended"), el("div", "you-said", said ? d.case.recommendation_given : "— no recommendation was recorded"));
      left.querySelector(".k").style.color = "var(--cyan-text)";
      const right = el("div", "col");
      right.append(el("span", "k", "what actually happened"), el("div", "real", b.historical_outcome));
      right.querySelector(".k").style.color = "var(--gold)";
      outcome.append(left, right);
    }

    // the debrief
    const box = $("#debrief");
    box.replaceChildren();
    if (d) box.append(...renderDebrief(d, s.mode));
    else if (current.debrief_md) { const md = el("div", "md"); md.append(...renderMarkdown(current.debrief_md)); box.append(md); }
    else box.append(el("div", "verdict", s.state === "ended" ? "The debrief is being written — check back in a minute." : "No debrief: the interview ended before you said anything."));

    // the code
    const codeBox = $("#review-code");
    const submitted = (current.code || []).filter((c) => c.event === "submit");
    const last = submitted[submitted.length - 1] || (current.code || [])[(current.code || []).length - 1];
    if (last) { codeBox.hidden = false; codeBox.querySelector("pre").textContent = `// ${last.lang}\n${last.code}`; }
    else codeBox.hidden = true;
  }

  function renderDebrief(d, mode) {
    const out = [];
    const scoreRow = el("div", "score-row");
    const score = el("div", "score");
    score.append(el("b", null, d.score_1_5 != null ? String(d.score_1_5) : "–"), el("span", null, "OF 5"));
    scoreRow.append(score, el("div", "verdict", d.overall || ""));
    out.push(scoreRow);

    if (Array.isArray(d.strengths) && d.strengths.length) {
      const box = el("div", "dlist"); box.append(el("span", "k good", "strengths"));
      const ul = el("ul"); for (const s of d.strengths) ul.append(el("li", null, s)); box.append(ul); out.push(box);
    }
    if (Array.isArray(d.improvements) && d.improvements.length) {
      const box = el("div", "dlist"); box.append(el("span", "k work", "work on · ranked"));
      const ol = el("ol");
      d.improvements.forEach((s, i) => { const li = el("li"); li.append(el("span", "n", String(i + 1)), el("span", null, s)); ol.append(li); });
      box.append(ol); out.push(box);
    }
    if (d.communication && typeof d.communication === "object") {
      const box = el("div", "dlist"); box.append(el("span", "k", "communication"));
      const dl = el("dl", "comm");
      for (const key of ["clarity", "structure", "pace"]) { if (d.communication[key]) dl.append(el("dt", null, key), el("dd", null, d.communication[key])); }
      box.append(dl); out.push(box);
    }
    if (mode === "case" && d.case && typeof d.case === "object" && d.case.vs_historical_outcome && d.case.vs_historical_outcome !== "n/a") {
      const box = el("div", "dlist"); box.append(el("span", "k", `against what really happened${d.case.structure_score_1_5 ? ` · structure ${d.case.structure_score_1_5}/5` : ""}`));
      box.append(el("div", "verdict", d.case.vs_historical_outcome)); out.push(box);
    }
    if (mode === "technical" && d.code && typeof d.code === "object" && d.code.correctness && d.code.correctness !== "n/a") {
      const box = el("div", "dlist"); box.append(el("span", "k", `the code${d.code.score_1_5 ? ` · ${d.code.score_1_5}/5` : ""}`));
      const dl = el("dl", "comm");
      for (const key of ["correctness", "complexity", "style"]) { if (d.code[key]) dl.append(el("dt", null, key), el("dd", null, d.code[key])); }
      box.append(dl); out.push(box);
    }
    if (Array.isArray(d.per_question) && d.per_question.length) {
      const box = el("div", "dlist"); box.append(el("span", "k", "per question"));
      const grid = el("div", "qgrid");
      const note = el("div", "qnote");
      const showNote = (q, i, btn) => {
        for (const b of grid.children) b.classList.toggle("on", b === btn);
        note.replaceChildren(el("span", "k", `Q${i + 1} · ${q.score_1_5 != null ? q.score_1_5 : "–"}`), document.createTextNode(`${q.q ? q.q + " — " : ""}${q.note || ""}`));
      };
      d.per_question.forEach((q, i) => {
        const btn = el("button");
        btn.type = "button";
        btn.append(el("b", null, q.score_1_5 != null ? String(q.score_1_5) : "–"), el("span", null, `Q${i + 1}`));
        btn.addEventListener("click", () => showNote(q, i, btn));
        grid.append(btn);
      });
      box.append(grid, note);
      out.push(box);
      const best = d.per_question.reduce((a, q, i) => (q.score_1_5 || 0) > (d.per_question[a].score_1_5 || 0) ? i : a, 0);
      showNote(d.per_question[best], best, grid.children[best]);
    }
    if (Array.isArray(d.next_practice) && d.next_practice.length) {
      const box = el("div", "dlist"); box.append(el("span", "k", "practise next"));
      const ul = el("ul"); for (const s of d.next_practice) ul.append(el("li", null, s)); box.append(ul); out.push(box);
    }
    return out;
  }

  function setupPlayer(player, rows, durationHint) {
    const toggle = $("#player-toggle"), bar = $("#player-bar"), fill = $("#player-fill"), head = $("#player-head");
    const total = () => (isFinite(player.duration) && player.duration > 0) ? player.duration : durationHint || 0;
    const place = () => {
      for (const t of bar.querySelectorAll(".tick")) t.remove();
      const dur = total();
      text("#player-total", fmtDuration(dur));
      let lastSpeaker = null;
      for (const r of rows) {
        const isTurn = r.speaker === "interviewer" && lastSpeaker !== "interviewer";
        const isEx = r.speaker === "event" && /^exhibit shared|^problem posed/.test(r.text || "");
        if ((isTurn || isEx) && dur) {
          const tick = el("span", "tick" + (isEx ? " ex" : ""));
          tick.style.left = `${Math.min(100, (r.t / dur) * 100)}%`;
          bar.append(tick);
        }
        if (r.speaker !== "event") lastSpeaker = r.speaker;
      }
    };
    player.onloadedmetadata = place;
    place();
    player.ontimeupdate = () => {
      const dur = total();
      const p = dur ? Math.min(100, (player.currentTime / dur) * 100) : 0;
      fill.style.width = `${p}%`; head.style.left = `${p}%`;
      text("#player-time", fmtDuration(player.currentTime));
      let active = null;
      const lines = $("#review-transcript").children;
      for (const line of lines) if (Number(line.dataset.t) <= player.currentTime) active = line;
      for (const line of lines) line.classList.toggle("playing", line === active);
    };
    player.onplay = () => { toggle.textContent = "❚❚"; };
    player.onpause = player.onended = () => { toggle.textContent = "▶"; };
    toggle.onclick = () => { if (player.paused) player.play(); else player.pause(); };
    bar.onclick = (e) => {
      const rect = bar.getBoundingClientRect();
      const dur = total();
      if (dur) { player.currentTime = ((e.clientX - rect.left) / rect.width) * dur; player.play(); }
    };
  }

  // A few markdown shapes — headings, bullets, bold — for a debrief that only has its text.
  function renderMarkdown(md) {
    const out = [];
    let list = null;
    const inline = (t) => {
      const frag = document.createDocumentFragment();
      for (const part of t.split(/(\*\*[^*]+\*\*)/g)) {
        if (part.startsWith("**") && part.endsWith("**")) frag.appendChild(el("strong", null, part.slice(2, -2)));
        else frag.appendChild(document.createTextNode(part));
      }
      return frag;
    };
    for (const raw of (md || "").split("\n")) {
      const line = raw.trimEnd();
      if (!line.trim()) { list = null; continue; }
      const h = /^(#{1,3})\s+(.*)$/.exec(line);
      if (h) { list = null; const e = el(`h${h[1].length + 1}`); e.appendChild(inline(h[2])); out.push(e); continue; }
      const li = /^[-*]\s+(.*)$/.exec(line);
      if (li) {
        if (!list) { list = el("ul"); out.push(list); }
        const e = el("li"); e.appendChild(inline(li[1])); list.appendChild(e); continue;
      }
      list = null;
      const p = el("p"); p.appendChild(inline(line)); out.push(p);
    }
    return out;
  }

  // ── admin ──────────────────────────────────────────────────────────────

  async function enterAdmin() {
    show("admin");
    flag($("#account-secret"), false);
    await refreshAccounts();
  }

  function showSecret(username, password, kind) {
    const box = $("#account-secret");
    box.dataset.kind = kind || "ok";
    text("#account-secret-k", kind === "error" ? "that did not work" : kind === "reset" ? "password reset · shown once" : "account created · password shown once");
    text("#account-secret-who", username);
    text("#account-secret-pw", password);
    flag(box, true);
  }
  $("#account-secret-done").addEventListener("click", () => flag($("#account-secret"), false));
  $("#account-secret-copy").addEventListener("click", async () => {
    const pw = $("#account-secret-pw").textContent;
    try {
      await navigator.clipboard.writeText(pw);
      text("#account-secret-copy", "Copied");
      setTimeout(() => text("#account-secret-copy", "Copy"), 1500);
    } catch (_) {}
  });

  async function refreshAccounts() {
    const list = $("#accounts");
    list.replaceChildren();
    let rows = [];
    try { rows = (await api("/admin/accounts")).accounts || []; } catch (ex) { return; }
    text("#accounts-count", rows.length);
    const tpl = $("#account-row");
    const weekAgo = Date.now() - 7 * 86400000;
    for (const a of rows) {
      const row = tpl.content.firstElementChild.cloneNode(true);
      row.dataset.username = a.username;
      row.dataset.disabled = a.disabled ? "1" : "0";
      row.dataset.recent = a.last_seen && new Date(a.last_seen) > weekAgo ? "1" : "0";
      row.querySelector("[data-field=username]").textContent = a.username;
      row.querySelector("[data-field=role]").textContent = a.disabled ? "disabled · sessions kept" : a.role === "admin" ? "owner" : (a.last_seen ? "" : "first sign-in pending");
      row.querySelector("[data-field=created]").textContent = shortDate(a.created);
      row.querySelector("[data-field=last_seen]").textContent = a.last_seen ? shortDate(a.last_seen) : "never";
      row.querySelector("[data-field=status]").textContent = a.disabled ? "disabled" : "active";
      row.querySelector("[data-action=disable]").hidden = a.disabled || a.username === me.username;
      row.querySelector("[data-action=enable]").hidden = !a.disabled;
      row.querySelector("[data-action=delete]").hidden = a.username === me.username;
      const u = encodeURIComponent(a.username);
      row.querySelector("[data-action=reset]").addEventListener("click", async () => {
        try { const r = await api(`/admin/accounts/${u}/reset`, { method: "POST", body: {} }); showSecret(a.username, r.password, "reset"); }
        catch (ex) { showSecret(a.username, ex.message, "error"); }
      });
      row.querySelector("[data-action=setpw]").addEventListener("click", async () => {
        const chosen = prompt(`New password for ${a.username} (8+ characters):`);
        if (!chosen) return;
        try { const r = await api(`/admin/accounts/${u}/reset`, { method: "POST", body: { password: chosen } }); showSecret(a.username, r.password, "reset"); }
        catch (ex) { showSecret(a.username, ex.message, "error"); }
      });
      const disableBtn = row.querySelector("[data-action=disable]");
      disableBtn.addEventListener("click", async () => {
        if (disableBtn.dataset.armed !== "1") {
          disableBtn.dataset.armed = "1"; disableBtn.textContent = "Sure?";
          setTimeout(() => { disableBtn.dataset.armed = "0"; disableBtn.textContent = "Disable"; }, 3000);
          return;
        }
        try { await api(`/admin/accounts/${u}/disable`, { method: "POST" }); } catch (ex) { showSecret(a.username, ex.message, "error"); }
        refreshAccounts();
      });
      row.querySelector("[data-action=enable]").addEventListener("click", async () => {
        try { await api(`/admin/accounts/${u}/enable`, { method: "POST" }); } catch (ex) { showSecret(a.username, ex.message, "error"); }
        refreshAccounts();
      });
      row.querySelector("[data-action=delete]").addEventListener("click", async () => {
        if (!confirm(`Delete account ${a.username}? Their sessions stay on disk.`)) return;
        try { await api(`/admin/accounts/${u}`, { method: "DELETE" }); } catch (ex) { showSecret(a.username, ex.message, "error"); }
        refreshAccounts();
      });
      list.appendChild(row);
    }
  }

  $("#account-create-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#account-new-name").value.trim().toLowerCase();
    if (!name) return;
    const chosen = $("#account-new-pass").value;
    try {
      const r = await api("/admin/accounts", { method: "POST", body: chosen ? { username: name, password: chosen } : { username: name } });
      showSecret(r.account.username, r.password, "created");
      $("#account-new-name").value = "";
      $("#account-new-pass").value = "";
    } catch (ex) {
      showSecret(name, ex.message, "error");
    }
    refreshAccounts();
  });

  // ── boot ───────────────────────────────────────────────────────────────

  (async () => {
    try {
      me = await api("/me");
      applyCaps();
      enterLobby();
    } catch (_) {
      show("login");
    }
  })();
})();
