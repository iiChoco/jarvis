// The interview room's behaviour — everything the page does, in one file,
// touching the DOM only through the ids, data attributes, and <template>s
// listed in interview.html (the contract). The designed page swaps in
// around this script unchanged.
//
// Views: sign in, lobby, setup, brief, live (socket, speech, playback,
// recording, the code editor), review, admin.
(() => {
  "use strict";

  const API = "/interview/api";
  const $ = (sel) => document.querySelector(sel);
  const body = document.body;

  let me = null; // {username, role, caps, limits}

  // ── views ──────────────────────────────────────────────────────────────

  function show(view) {
    body.dataset.view = view;
    $("#nav-lobby").hidden = !me || view === "lobby";
    $("#nav-admin").hidden = !me || me.role !== "admin" || view === "admin";
    $("#nav-logout").hidden = !me;
    $("#whoami").textContent = me ? me.username : "";
  }

  function flag(el, on) { el.dataset.show = on ? "1" : "0"; }

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
    if (!res.ok) {
      const reason = (data && data.reason) || res.statusText || "request failed";
      throw new Error(reason);
    }
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
        body: { username: $("#login-user").value.trim(), password: $("#login-pass").value },
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

  function applyCaps() {
    const caps = (me && me.caps) || {};
    body.dataset.tts = caps.tts || "browser";
    body.dataset.stt = ("SpeechRecognition" in window || "webkitSpeechRecognition" in window)
      ? "browser" : "none";
  }

  // ── lobby ──────────────────────────────────────────────────────────────

  async function enterLobby() {
    show("lobby");
    const list = $("#sessions");
    list.replaceChildren();
    let sessions = [];
    try {
      const data = await api("/sessions");
      sessions = data.sessions || [];
    } catch (_) {
      sessions = [];
    }
    flag($("#sessions-empty"), sessions.length === 0);
    const tpl = $("#session-row");
    for (const s of sessions) {
      const row = tpl.content.firstElementChild.cloneNode(true);
      row.dataset.id = s.id;
      row.querySelector("[data-field=date]").textContent = (s.created || "").replace("T", " ").slice(0, 16);
      row.querySelector("[data-field=title]").textContent = s.title || "(untitled)";
      row.querySelector("[data-field=mode]").textContent = s.mode || "";
      row.querySelector("[data-field=duration]").textContent = fmtDuration(s.duration_s);
      row.querySelector("[data-field=debrief]").textContent = s.debriefed ? "debrief ready" : s.state || "";
      row.querySelector("[data-action=open]").addEventListener("click", () => openSession(s));
      row.querySelector("[data-action=delete]").addEventListener("click", async () => {
        if (!confirm("Delete this interview and its recording?")) return;
        try { await api(`/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" }); } catch (_) {}
        enterLobby();
      });
      list.appendChild(row);
    }
  }

  function fmtDuration(s) {
    if (!s && s !== 0) return "";
    const m = Math.floor(s / 60), sec = Math.floor(s % 60);
    return `${m}:${String(sec).padStart(2, "0")}`;
  }

  $("#new-interview").addEventListener("click", () => show("setup"));

  let current = null; // {session, setup, brief, transcript, code, debrief, debrief_md}

  async function openSession(s) {
    try {
      current = await api(`/sessions/${encodeURIComponent(s.id)}`);
    } catch (ex) { return; }
    const state = current.session.state;
    if (state === "prepared") renderBrief();
    else if (state === "live") enterLive();
    else enterReview();
  }

  // ── setup ──────────────────────────────────────────────────────────────

  for (const radio of document.querySelectorAll("#setup-form [name=mode]")) {
    radio.addEventListener("change", () => { body.dataset.setupMode = radio.value; });
  }

  $("#setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = $("#setup-form");
    const mode = (form.querySelector("[name=mode]:checked") || {}).value || "company";
    const val = (sel) => { const el = form.querySelector(sel); return el ? el.value : ""; };
    const setup = {
      mode,
      request: $("#setup-request").value,
      role: $("#setup-role").value,
      seniority: $("#setup-seniority").value,
      focus: ($("#setup-focus") || {}).value || "",
      length_min: Number(val("[name=length]") || 30),
      case_type: val("[name=case_type]") || "any",
      case_source: val("[name=case_source]") || "library",
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
    for (const el of document.querySelectorAll(`[data-view=brief] [data-field=${name}]`)) {
      el.textContent = Array.isArray(value) ? value.join("; ") : (value == null ? "" : String(value));
    }
  }

  function renderBrief() {
    show("brief");
    const b = current.brief || {};
    const mode = current.session.mode;
    for (const el of document.querySelectorAll("[data-view=brief] [data-field]")) el.textContent = "";
    $("#brief-questions").replaceChildren();
    if (mode === "case") {
      setField("name", b.title);
      setField("tagline", b.client);
      setField("case_prompt", b.prompt);
      setField("case_client", b.client);
      setField("case_type", b.type);
      setField("case_background", "Ask the interviewer — background is given on request.");
    } else {
      const c = b.company || {}, r = b.role || {}, who = b.interviewer || {};
      setField("name", c.name);
      setField("tagline", c.tagline);
      for (const k of ["industry", "stage", "size", "hq", "founded", "business_model", "culture", "products", "recent_news"]) setField(k, c[k]);
      setField("role_title", r.title);
      setField("role_level", r.level);
      setField("role_team", r.team);
      setField("role_responsibilities", r.responsibilities);
      setField("role_must_haves", r.must_haves);
      setField("interviewer", who.name ? `${who.name}, ${who.title} — ${who.style}` : "");
      if (mode === "technical" && b.technical) {
        setField("tech_focus", b.technical.focus);
        setField("tech_expect", b.technical.what_to_expect);
      }
      const tpl = $("#question-row");
      for (const q of b.likely_questions || []) {
        const row = tpl.content.firstElementChild.cloneNode(true);
        row.querySelector("[data-field=q]").textContent = q.q || "";
        row.querySelector("[data-field=kind]").textContent = q.kind || "";
        $("#brief-questions").appendChild(row);
      }
    }
    $("#brief-regenerate").disabled = false;
  }

  $("#brief-regenerate").addEventListener("click", async () => {
    if (!current) return;
    $("#brief-regenerate").disabled = true;
    try {
      const r = await api(`/sessions/${encodeURIComponent(current.session.id)}/regenerate`, { method: "POST" });
      current.session = r.session; current.brief = r.brief;
      renderBrief();
    } catch (ex) {
      alert(ex.message);
      $("#brief-regenerate").disabled = false;
    }
  });

  $("#brief-begin").addEventListener("click", () => enterLive());

  // ── live ───────────────────────────────────────────────────────────────
  //
  // One socket per interview. The hub owns every decision about whose turn
  // it is; this side reports what the microphone hears, plays what the
  // interviewer says, records the mix, and mirrors state onto <body>.

  const live = {
    ws: null, ended: false, closing: false, turn: 0, state: "idle",
    audio: null, mix: null, queue: [], playing: false, turnEnded: false,
    recorder: null, recSeq: 0, recognition: null, muted: false, recOn: false,
    partialAt: 0, timer: null, elapsedBase: 0, elapsedFrom: 0, questions: 0,
    problem: null, starterShown: "", snapshotTimer: null, cm: null, retry: null,
    debriefReady: false, holdAnim: null,
  };

  const hasSTT = () => "SpeechRecognition" in window || "webkitSpeechRecognition" in window;
  const hasAudio = () => "AudioContext" in window || "webkitAudioContext" in window;

  async function enterLive() {
    if (!current) return;
    show("live");
    body.dataset.state = "idle";
    body.dataset.rec = "off";
    live.ended = false; live.closing = false; live.turn = 0; live.questions = 0;
    live.queue = []; live.playing = false; live.debriefReady = false; live.problem = null;
    $("#q-text").textContent = "";
    $("#partial").textContent = "";
    $("#transcript").replaceChildren();
    $("#exhibits").replaceChildren();
    $("#problem").hidden = true;
    flag($("#apology"), false);
    $("#q-count").textContent = "Q0";
    $("#timer").textContent = "00:00";
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
        live.turn = f.turn;
        live.elapsedBase = f.elapsed_s; live.elapsedFrom = performance.now();
        startTimer();
        for (const h of f.history || []) {
          if (h.type === "say") appendQuestion(h.turn, h.text);
          else if (h.type === "exhibit") renderExhibit(h);
          else if (h.type === "problem") renderProblem(h);
          else if (h.type === "transcript") addTranscript($("#transcript"), h);
        }
        setState(f.state, 0);
        break;
      case "state":
        setState(f.state, f.hold_ms || 0);
        break;
      case "say":
        if (f.turn !== live.turn) { live.turn = f.turn; $("#q-text").textContent = ""; live.turnEnded = false; }
        appendQuestion(f.turn, f.text);
        enqueueSpeech(f);
        break;
      case "turn.end":
        live.turnEnded = true;
        live.questions = f.turn - 1;
        $("#q-count").textContent = `Q${Math.max(0, live.questions)}`;
        maybeReportPlayed();
        break;
      case "exhibit": renderExhibit(f); break;
      case "problem": renderProblem(f); break;
      case "apology":
        stopPlayback();
        $("#apology").textContent = f.text;
        flag($("#apology"), true);
        setTimeout(() => flag($("#apology"), false), 4000);
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

  function setState(state, holdMs) {
    live.state = state;
    body.dataset.state = state;
    $("#state").textContent = state;
    if (state === "listening") { animateHold(holdMs); startRecognition(); }
    else stopHold();
    if (state === "thinking") $("#partial").textContent = "";
    if (state === "ended") {
      live.ended = true;
      stopRecognition();
      // Give the debrief a moment; finishLive() runs when it arrives, or now if it already did.
      if (live.debriefReady) finishLive();
      else setTimeout(() => { if (live.ended && body.dataset.view === "live") finishLive(); }, 90000);
    }
  }

  function appendQuestion(turn, text) {
    const el = $("#q-text");
    el.textContent = (el.textContent ? el.textContent + " " : "") + text;
  }

  function addTranscript(container, row) {
    const tpl = $("#transcript-row");
    const el = tpl.content.firstElementChild.cloneNode(true);
    el.dataset.speaker = row.speaker;
    el.dataset.t = row.t;
    el.querySelector("[data-field=t]").textContent = fmtDuration(row.t);
    el.querySelector("[data-field=text]").textContent = row.text;
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
    return el;
  }

  function renderExhibit(f) {
    const tpl = $("#exhibit-card");
    const card = tpl.content.firstElementChild.cloneNode(true);
    card.dataset.id = f.id;
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
    }
    $("#exhibits").prepend(card);
  }

  // ── timer & hold ring ──────────────────────────────────────────────────

  function startTimer() {
    if (live.timer) clearInterval(live.timer);
    live.timer = setInterval(() => {
      const s = live.elapsedBase + (performance.now() - live.elapsedFrom) / 1000;
      const el = $("#timer");
      if (el) el.textContent = fmtDuration(s).padStart(5, "0");
    }, 500);
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
        const text = r[0].transcript.trim();
        if (!text) continue;
        if (r.isFinal) {
          send({ type: "speech.final", text, t: nowS() });
        } else {
          interim += (interim ? " " : "") + text;
        }
      }
      if (live.state === "thinking" || live.state === "speaking") {
        // The candidate is talking while the room is: tell it at once.
        stopPlayback();
        send({ type: "barge", t: nowS() });
      }
      $("#partial").textContent = interim;
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
      if (!live.ended && !live.muted && !live.playing && body.dataset.view === "live") {
        setTimeout(startRecognition, 150);
      }
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
    $("#btn-mute").textContent = live.muted ? "Unmute" : "Mute";
    if (live.muted) stopRecognition(); else startRecognition();
  });

  $("#btn-done").addEventListener("click", () => {
    stopPlayback();
    send({ type: "answer.done" });
  });

  $("#typed-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#typed-text").value.trim();
    if (!text) return;
    stopPlayback();
    send({ type: "typed", text });
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
  function setEditor(text) { if (live.cm) live.cm.setValue(text); else $("#code-editor").value = text; }

  function renderProblem(f) {
    live.problem = f;
    const p = $("#problem");
    p.hidden = false;
    p.querySelector("[data-field=title]").textContent = f.title;
    p.querySelector("[data-field=meta]").textContent = [f.difficulty, f.topic].filter(Boolean).join(" · ");
    p.querySelector("[data-field=statement]").textContent = f.statement;
    const ex = p.querySelector("[data-field=examples]"); ex.replaceChildren();
    for (const e of f.examples || []) {
      const li = document.createElement("li");
      li.textContent = `Input: ${e.input}  →  Output: ${e.output}${e.note ? `  (${e.note})` : ""}`;
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

  function enterReview() {
    show("review");
    const s = current.session, md = current.debrief_md;
    document.querySelector("[data-view=review] [data-field=title]").textContent = s.title || "";
    document.querySelector("[data-view=review] [data-field=meta]").textContent =
      `${(s.created || "").replace("T", " ").slice(0, 16)} · ${s.mode} · ${fmtDuration(s.duration_s)} · ${s.question_count || 0} questions`;
    const player = $("#player");
    if (s.has_recording) { player.hidden = false; player.src = `${API}/sessions/${encodeURIComponent(s.id)}/recording`; }
    else { player.hidden = true; player.removeAttribute("src"); }
    $("#debrief").replaceChildren(...renderMarkdown(md || (s.state === "debriefed" ? "" : "The debrief is not ready yet — check back in a minute.")));
    const outcome = $("#review-outcome");
    outcome.replaceChildren();
    if (s.mode === "case" && current.brief && current.brief.historical_outcome) {
      const h = document.createElement("h3"); h.textContent = "What really happened";
      const p = document.createElement("p"); p.textContent = current.brief.historical_outcome;
      outcome.append(h, p);
    }
    const codeBox = $("#review-code");
    const submitted = (current.code || []).filter((c) => c.event === "submit");
    const last = submitted[submitted.length - 1] || (current.code || [])[(current.code || []).length - 1];
    if (last) { codeBox.hidden = false; codeBox.querySelector("pre").textContent = `// ${last.lang}\n${last.code}`; }
    else codeBox.hidden = true;
    const tr = $("#review-transcript");
    tr.replaceChildren();
    for (const row of current.transcript || []) {
      const el = addTranscript(tr, row);
      el.addEventListener("click", () => { if (s.has_recording) { player.currentTime = row.t; player.play(); } });
    }
  }

  // A few markdown shapes — headings, bullets, bold — from the debrief; anything else is a paragraph.
  function renderMarkdown(md) {
    const out = [];
    let list = null;
    const inline = (text) => {
      const frag = document.createDocumentFragment();
      const parts = text.split(/(\*\*[^*]+\*\*)/g);
      for (const part of parts) {
        if (part.startsWith("**") && part.endsWith("**")) { const b = document.createElement("strong"); b.textContent = part.slice(2, -2); frag.appendChild(b); }
        else frag.appendChild(document.createTextNode(part));
      }
      return frag;
    };
    for (const raw of (md || "").split("\n")) {
      const line = raw.trimEnd();
      if (!line.trim()) { list = null; continue; }
      const h = /^(#{1,3})\s+(.*)$/.exec(line);
      if (h) { list = null; const el = document.createElement(`h${h[1].length + 1}`); el.appendChild(inline(h[2])); out.push(el); continue; }
      const li = /^[-*]\s+(.*)$/.exec(line);
      if (li) {
        if (!list) { list = document.createElement("ul"); out.push(list); }
        const el = document.createElement("li"); el.appendChild(inline(li[1])); list.appendChild(el); continue;
      }
      list = null;
      const p = document.createElement("p"); p.appendChild(inline(line)); out.push(p);
    }
    return out;
  }

  // ── admin ──────────────────────────────────────────────────────────────

  async function enterAdmin() {
    show("admin");
    $("#account-secret").textContent = "";
    await refreshAccounts();
  }

  async function refreshAccounts() {
    const list = $("#accounts");
    list.replaceChildren();
    let rows = [];
    try { rows = (await api("/admin/accounts")).accounts || []; } catch (ex) { return; }
    const tpl = $("#account-row");
    for (const a of rows) {
      const row = tpl.content.firstElementChild.cloneNode(true);
      row.dataset.username = a.username;
      row.dataset.disabled = a.disabled ? "1" : "0";
      row.querySelector("[data-field=username]").textContent = a.username;
      row.querySelector("[data-field=role]").textContent = a.role;
      row.querySelector("[data-field=created]").textContent = (a.created || "").slice(0, 10);
      row.querySelector("[data-field=last_seen]").textContent = a.last_seen ? a.last_seen.slice(0, 10) : "never";
      row.querySelector("[data-field=status]").textContent = a.disabled ? "disabled" : "active";
      row.querySelector("[data-action=disable]").hidden = a.disabled;
      row.querySelector("[data-action=enable]").hidden = !a.disabled;
      const u = encodeURIComponent(a.username);
      row.querySelector("[data-action=reset]").addEventListener("click", async () => {
        try {
          const r = await api(`/admin/accounts/${u}/reset`, { method: "POST" });
          showSecret(a.username, r.password);
        } catch (ex) { showSecret("error", ex.message); }
      });
      row.querySelector("[data-action=disable]").addEventListener("click", async () => {
        try { await api(`/admin/accounts/${u}/disable`, { method: "POST" }); } catch (ex) { showSecret("error", ex.message); }
        refreshAccounts();
      });
      row.querySelector("[data-action=enable]").addEventListener("click", async () => {
        try { await api(`/admin/accounts/${u}/enable`, { method: "POST" }); } catch (ex) { showSecret("error", ex.message); }
        refreshAccounts();
      });
      row.querySelector("[data-action=delete]").addEventListener("click", async () => {
        if (!confirm(`Delete account ${a.username}?`)) return;
        try { await api(`/admin/accounts/${u}`, { method: "DELETE" }); } catch (ex) { showSecret("error", ex.message); }
        refreshAccounts();
      });
      list.appendChild(row);
    }
  }

  function showSecret(username, password) {
    $("#account-secret").textContent = `${username}: ${password}`;
  }

  $("#account-create-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#account-new-name").value.trim().toLowerCase();
    if (!name) return;
    try {
      const r = await api("/admin/accounts", { method: "POST", body: { username: name } });
      showSecret(r.account.username, r.password);
      $("#account-new-name").value = "";
    } catch (ex) {
      showSecret("error", ex.message);
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
