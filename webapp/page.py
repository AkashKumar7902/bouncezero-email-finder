"""The single self-contained HTML page for the hosted web app.

Inline HTML + CSS + vanilla JS, NO external / CDN URLs (kept self-contained so
the page works under a strict CSP and offline). The style is lifted from
``emailfinder.web``'s inline page, trimmed to the hosted surface: a single-lookup
card (provider badge + 0-100 confidence bar + status chip + honest cap note +
'why this guess') and a public, no-login opt-out form. The JS talks only to the
same-origin JSON endpoints ``/api/find`` and ``/api/optout``.
"""
from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BounceZero — hosted email finder</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#ffffff; --ink:#1a1d23; --muted:#5c6470;
    --line:#e3e6ea; --accent:#2f6df6; --accent-ink:#ffffff;
    --ok:#188a4a; --ok-bg:#e4f5ea; --warn:#9a6a00; --warn-bg:#fff2d6;
    --bad:#b3261e; --bad-bg:#fbe3e1; --unk:#5c6470; --unk-bg:#eceef1;
    --shadow:0 1px 3px rgba(0,0,0,.08);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0f1216; --card:#171b21; --ink:#e8eaed; --muted:#9aa3af;
      --line:#262c34; --accent:#5b8bff; --accent-ink:#0f1216;
      --ok:#5fd18b; --ok-bg:#123222; --warn:#e6bf6a; --warn-bg:#33280f;
      --bad:#f2938c; --bad-bg:#3a1917; --unk:#9aa3af; --unk-bg:#20262e;
      --shadow:0 1px 3px rgba(0,0,0,.5);
    }
  }
  *{box-sizing:border-box}
  html,body{max-width:100%;overflow-x:hidden}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  header{padding:22px 20px 6px;max-width:820px;margin:0 auto}
  h1{margin:0;font-size:22px;letter-spacing:-.2px}
  .tag{color:var(--muted);font-size:13px;margin-top:2px}
  main{max-width:820px;margin:0 auto;padding:12px 20px 60px;
    display:grid;gap:18px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:18px;box-shadow:var(--shadow)}
  .card h2{margin:0 0 12px;font-size:15px;text-transform:uppercase;
    letter-spacing:.06em;color:var(--muted)}
  label{display:block;font-size:13px;color:var(--muted);margin:8px 0 4px}
  input[type=text],select{width:100%;padding:9px 10px;border-radius:8px;
    border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .row>div{flex:1 1 200px;min-width:0}
  button{cursor:pointer;font:inherit;border:0;border-radius:8px;padding:9px 16px;
    background:var(--accent);color:var(--accent-ink);font-weight:600}
  button.ghost{background:transparent;color:var(--accent);
    border:1px solid var(--line)}
  button:disabled{opacity:.5;cursor:default}
  .actions{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}
  .badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;
    font-weight:600;background:var(--unk-bg);color:var(--unk)}
  .chip{display:inline-block;padding:3px 10px;border-radius:6px;font-size:12px;
    font-weight:700;text-transform:uppercase;letter-spacing:.04em}
  .chip.deliverable{background:var(--ok-bg);color:var(--ok)}
  .chip.risky{background:var(--warn-bg);color:var(--warn)}
  .chip.undeliverable{background:var(--bad-bg);color:var(--bad)}
  .chip.unknown{background:var(--unk-bg);color:var(--unk)}
  .email{font-size:20px;font-weight:700;word-break:break-all;margin:6px 0}
  .bar{height:10px;border-radius:999px;background:var(--unk-bg);overflow:hidden;
    margin:8px 0}
  .bar>i{display:block;height:100%;background:var(--accent)}
  .cap{color:var(--warn);font-size:13px;margin-top:6px;font-weight:600}
  .meta{color:var(--muted);font-size:13px;margin-top:8px}
  .why{margin-top:12px}
  .why summary{cursor:pointer;color:var(--accent);font-weight:600;font-size:13px}
  .why ul{margin:8px 0 0;padding-left:18px}
  .why li{margin:2px 0;font-size:13px}
  .alt{border-top:1px solid var(--line);margin-top:14px;padding-top:12px}
  .err{color:var(--bad);font-size:13px;margin-top:8px}
  .hint{color:var(--muted);font-size:12px;margin-top:4px}
  .note{color:var(--muted);font-size:12px;margin-top:6px}
</style>
</head>
<body>
<header>
  <h1>BounceZero</h1>
  <div class="tag">hosted, provider-aware email finder — pattern + DNS only, no SMTP verification</div>
</header>
<main>

  <section class="card" id="lookup">
    <h2>Single lookup</h2>
    <div class="row">
      <div>
        <label for="name">Full name</label>
        <input type="text" id="name" placeholder="Ajith Kumar" autocomplete="off">
      </div>
      <div>
        <label for="ttype">Target</label>
        <select id="ttype">
          <option value="domain">Domain</option>
          <option value="company">Company</option>
          <option value="linkedin">LinkedIn URL</option>
        </select>
      </div>
      <div>
        <label for="target" id="tlabel">Domain</label>
        <input type="text" id="target" placeholder="acme.com" autocomplete="off">
        <div class="hint" id="thint">e.g. acme.com</div>
      </div>
    </div>
    <div class="actions">
      <button id="findbtn">Find email</button>
    </div>
    <div class="note">Verification is off on the hosted app — results are
      pattern + provider-aware only (Microsoft 365 and catch-all domains are
      never labelled deliverable).</div>
    <div id="findout"></div>
  </section>

  <section class="card" id="optcard">
    <h2>Opt out</h2>
    <div class="note">Ask us never to derive or return your address. No login required.</div>
    <div class="row" style="margin-top:8px">
      <div>
        <label for="oemail">Email</label>
        <input type="text" id="oemail" placeholder="you@company.com" autocomplete="off">
      </div>
      <div>
        <label for="oname">or Name</label>
        <input type="text" id="oname" placeholder="Jane Doe" autocomplete="off">
      </div>
      <div>
        <label for="odomain">+ Domain</label>
        <input type="text" id="odomain" placeholder="company.com" autocomplete="off">
      </div>
    </div>
    <div class="actions">
      <button id="optbtn">Submit opt-out</button>
    </div>
    <div id="optout"></div>
  </section>

</main>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function postJSON(url, body){
  return fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)});
}

const HINTS = {domain:"e.g. acme.com", company:"e.g. Acme Corp",
  linkedin:"e.g. linkedin.com/in/your-name"};
$("ttype").addEventListener("change", () => {
  const t = $("ttype").value;
  $("tlabel").textContent = t === "linkedin" ? "LinkedIn URL"
    : (t.charAt(0).toUpperCase() + t.slice(1));
  $("thint").textContent = HINTS[t];
});

function statusChip(sc){
  const s = (sc.status || "unknown");
  return `<span class="chip ${esc(s)}">${esc(s)}</span>`;
}

function renderCandidate(sc, provLabel){
  if(!sc) return "";
  const pct = Math.max(0, Math.min(100, sc.score|0));
  const reasons = (sc.reasons||[]).map(r => `<li>${esc(r)}</li>`).join("");
  const cap = sc.cap_note ? `<div class="cap">⚠ ${esc(sc.cap_note)}</div>` : "";
  const prov = `<span class="badge">${esc(provLabel)}</span>`;
  const flags = [];
  if(sc.is_catch_all) flags.push("catch-all");
  if(sc.is_role) flags.push("role");
  if(sc.is_disposable) flags.push("disposable");
  if(sc.webmail) flags.push("webmail");
  const flagstr = flags.length ? " · " + flags.join(", ") : "";
  return `
    <div class="email">${esc(sc.email)}</div>
    <div>${prov} ${statusChip(sc)} <span class="meta">${pct}/100${esc(flagstr)}</span></div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    ${cap}
    <div class="meta">template <b>${esc(sc.template)}</b> · separator
      "${esc(sc.separator)}"</div>
    <details class="why"><summary>Why this guess</summary>
      <ul>${reasons || "<li>no reasons recorded</li>"}</ul>
    </details>`;
}

async function doFind(){
  const btn = $("findbtn"); btn.disabled = true;
  const out = $("findout"); out.innerHTML = '<div class="meta">searching…</div>';
  const body = {name: $("name").value};
  const t = $("ttype").value; const v = $("target").value.trim();
  if(t === "domain") body.domain = v;
  else if(t === "company") body.company = v;
  else body.linkedin_url = v;
  try{
    const r = await postJSON("/api/find", body);
    const j = await r.json();
    if(!r.ok){ out.innerHTML = `<div class="err">${esc(j.detail||j.error||"error")}</div>`; return; }
    if(j.suppressed){
      out.innerHTML = `<div class="chip undeliverable">suppressed</div>
        <div class="meta">This identity opted out — no address returned.</div>`;
      return;
    }
    if(!j.best){
      const notes = (j.notes||[]).map(n=>esc(n)).join("; ");
      out.innerHTML = `<div class="meta">No candidate. ${notes}</div>`;
      return;
    }
    let html = renderCandidate(j.best, j.provider_label);
    html += `<div class="meta">provider: ${esc(j.provider_label)} · strategy:
      ${esc(j.strategy)} · verification: ${esc(j.verification_mode)}</div>`;
    if(j.alternates && j.alternates.length){
      html += `<div class="alt"><div class="meta">Alternates</div>`;
      for(const a of j.alternates){
        html += `<div style="margin-top:8px">${esc(a.email)} — ${statusChip(a)}
          <span class="meta">${a.score|0}/100</span></div>`;
      }
      html += `</div>`;
    }
    out.innerHTML = html;
  }catch(e){ out.innerHTML = `<div class="err">${esc(e)}</div>`; }
  finally{ btn.disabled = false; }
}

async function doOptout(){
  const out = $("optout");
  const body = {email: $("oemail").value, name: $("oname").value,
    domain: $("odomain").value};
  try{
    const r = await postJSON("/api/optout", body);
    if(r.status === 204){
      out.innerHTML = '<div class="chip deliverable">recorded</div>' +
        '<div class="meta">You have been added to the suppression list.</div>';
      $("oemail").value = $("oname").value = $("odomain").value = "";
    }else{
      const j = await r.json().catch(()=>({}));
      out.innerHTML = `<div class="err">${esc(j.detail||j.error||"failed")}</div>`;
    }
  }catch(e){ out.innerHTML = `<div class="err">${esc(e)}</div>`; }
}

$("findbtn").addEventListener("click", doFind);
$("name").addEventListener("keydown", e => { if(e.key==="Enter") doFind(); });
$("target").addEventListener("keydown", e => { if(e.key==="Enter") doFind(); });
$("optbtn").addEventListener("click", doOptout);
</script>
</body>
</html>
"""
