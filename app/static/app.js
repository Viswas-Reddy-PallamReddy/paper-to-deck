/* Paper -> Deck : front-end logic
   upload -> poll job -> view deck -> ask by voice */

const el = (id) => document.getElementById(id);
const esc = (s) => (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

let file = null;
let deckId = null;
let deck = null;
let idx = 0;
let rec = null;
let chunks = [];

/* ---------- languages ---------- */
fetch("/api/languages")
  .then((r) => r.json())
  .then((langs) => {
    const sel = el("lang");
    Object.entries(langs).forEach(([code, name]) => {
      const o = document.createElement("option");
      o.value = code;
      o.textContent = name;
      sel.appendChild(o);
    });
    sel.value = "en-IN";
  })
  .catch(() => {});

/* ---------- file pick / drop ---------- */
el("drop").addEventListener("click", () => el("file").click());
el("file").addEventListener("change", (e) => setFile(e.target.files[0]));
["dragover", "dragenter"].forEach((ev) =>
  el("drop").addEventListener(ev, (e) => {
    e.preventDefault();
    el("drop").classList.add("over");
  })
);
["dragleave", "drop"].forEach((ev) =>
  el("drop").addEventListener(ev, (e) => {
    e.preventDefault();
    el("drop").classList.remove("over");
  })
);
el("drop").addEventListener("drop", (e) => {
  if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
});

function setFile(f) {
  file = f || null;
  el("file-name").textContent = f ? f.name : "";
  el("go").disabled = !f;
}

/* ---------- generate ---------- */
el("go").addEventListener("click", startGenerate);

async function startGenerate() {
  if (!file) return;
  el("go").disabled = true;
  el("error").classList.add("hidden");
  el("progress").classList.remove("hidden");
  el("bar").style.width = "2%";
  el("stage").textContent = "Uploading…";

  const fd = new FormData();
  fd.append("file", file);
  fd.append("lang", el("lang").value);
  // checked -> always OCR with Sarvam Vision; unchecked -> auto (OCR only scans)
  fd.append("ocr", el("ocr").checked ? "always" : "auto");

  try {
    const r = await fetch("/api/generate", { method: "POST", body: fd });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || "upload failed");
    }
    const { job_id } = await r.json();
    deckId = job_id;
    poll(job_id);
  } catch (e) {
    fail(e.message);
  }
}

function fail(msg) {
  el("error").textContent = msg;
  el("error").classList.remove("hidden");
  el("progress").classList.add("hidden");
  el("go").disabled = false;
}

async function poll(jobId) {
  try {
    const r = await fetch(`/api/jobs/${jobId}`);
    const j = await r.json();
    if (j.status === "error") return fail(j.error || "generation failed");
    el("bar").style.width = (j.pct || 0) + "%";
    el("stage").textContent = j.message || "Working…";
    if (j.status === "done") {
      const d = await (await fetch(`/api/decks/${jobId}/deck.json`)).json();
      return showDeck(d);
    }
    setTimeout(() => poll(jobId), 1200);
  } catch (e) {
    fail(e.message);
  }
}

/* ---------- deck view ---------- */
function showDeck(d) {
  deck = d;
  idx = 0;
  el("upload-view").classList.add("hidden");
  el("deck-view").classList.remove("hidden");
  el("new-btn").classList.remove("hidden");

  const dots = el("dots");
  dots.innerHTML = "";
  d.slides.forEach((_, i) => {
    const dot = document.createElement("div");
    dot.className = "dot";
    dot.addEventListener("click", () => go(i));
    dots.appendChild(dot);
  });
  go(0);
}

function go(i) {
  if (!deck) return;
  idx = Math.max(0, Math.min(deck.slides.length - 1, i));
  const s = deck.slides[idx];

  el("slide-title").textContent = s.title;
  el("slide-count").textContent = `Slide ${idx + 1} of ${deck.slides.length}`;
  el("slide-bullets").innerHTML = (s.bullets || []).map((b) => `<li>${esc(b)}</li>`).join("");
  el("slide-visual").innerHTML = s.visual || "";

  const audio = el("slide-audio");
  audio.src = s.audio;
  audio.play().catch(() => {}); // autoplay may be blocked; the control is there

  [...el("dots").children].forEach((d, k) => d.classList.toggle("on", k === idx));
  el("prev").disabled = idx === 0;
  el("next").disabled = idx === deck.slides.length - 1;

  el("qa-out").innerHTML = "";
  el("qa-status").textContent = "";
}

el("prev").addEventListener("click", () => go(idx - 1));
el("next").addEventListener("click", () => go(idx + 1));
el("new-btn").addEventListener("click", () => location.reload());
document.addEventListener("keydown", (e) => {
  if (el("deck-view").classList.contains("hidden")) return;
  if (e.key === "ArrowRight") go(idx + 1);
  if (e.key === "ArrowLeft") go(idx - 1);
});

/* ---------- voice Q&A ---------- */
const askBtn = el("ask");
askBtn.addEventListener("mousedown", startRec);
askBtn.addEventListener("mouseup", stopRec);
askBtn.addEventListener("mouseleave", () => { if (rec) stopRec(); });
askBtn.addEventListener("touchstart", (e) => { e.preventDefault(); startRec(); });
askBtn.addEventListener("touchend", (e) => { e.preventDefault(); stopRec(); });

async function startRec() {
  if (rec || !deck) return;
  el("qa-status").textContent = "listening…";
  askBtn.classList.add("live");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    const src = ctx.createMediaStreamSource(stream);
    const node = ctx.createScriptProcessor(4096, 1, 1);
    chunks = [];
    node.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    src.connect(node);
    node.connect(ctx.destination);
    rec = { stream, ctx, src, node };
  } catch (e) {
    el("qa-status").textContent = "mic blocked";
    askBtn.classList.remove("live");
  }
}

async function stopRec() {
  if (!rec) return;
  const r = rec;
  rec = null;
  r.node.disconnect();
  r.src.disconnect();
  r.stream.getTracks().forEach((t) => t.stop());
  askBtn.classList.remove("live");
  el("qa-status").textContent = "thinking…";

  const b64 = await blobB64(encodeWav(chunks, 16000));
  const s = deck.slides[idx];

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        deck_id: deckId,
        audio: b64,
        slide_title: s.title,
        slide_text: `${s.title}\n${(s.bullets || []).join(" ")}\n${s.narration || ""}`,
      }),
    });
    const j = await res.json();
    if (!res.ok) throw new Error(j.detail || j.error || "ask failed");

    el("qa-status").textContent = "";
    let html = `<div class="q">You asked: ${esc(j.transcript)}</div>`;
    html += `<div>${esc(j.spoken)}</div>`;
    if (j.detail) html += `<div class="detail">${esc(j.detail)}</div>`;
    if (j.source) html += `<div class="src">Source: ${esc(j.source)}</div>`;
    el("qa-out").innerHTML = html;
    if (j.audio) new Audio("data:audio/wav;base64," + j.audio).play();
  } catch (e) {
    el("qa-status").textContent = "failed";
    el("qa-out").textContent = String(e);
  }
}

/* ---------- helpers ---------- */
function encodeWav(samples, rate) {
  let len = 0;
  for (const c of samples) len += c.length;
  const buf = new ArrayBuffer(44 + len * 2);
  const v = new DataView(buf);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); v.setUint32(4, 36 + len * 2, true); ws(8, "WAVE");
  ws(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  ws(36, "data"); v.setUint32(40, len * 2, true);
  let o = 44;
  for (const c of samples) {
    for (let i = 0; i < c.length; i++) {
      const x = Math.max(-1, Math.min(1, c[i]));
      v.setInt16(o, x < 0 ? x * 0x8000 : x * 0x7fff, true);
      o += 2;
    }
  }
  return new Blob([buf], { type: "audio/wav" });
}

function blobB64(blob) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result.split(",")[1]);
    r.onerror = rej;
    r.readAsDataURL(blob);
  });
}
