/* Dashboard del bot BTC · sin librerías externas. Todo el texto del servidor se inserta con textContent (sin XSS). */
"use strict";

const TOKEN = document.querySelector('meta[name="token"]').content;
const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v; else if (k === "text") el.textContent = v; else el.setAttribute(k, v);
  }
  for (const kid of kids) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
const usd = (x, nd = 2) => (x == null || isNaN(x)) ? "–" : "$" + Number(x).toLocaleString("en-US", { minimumFractionDigits: nd, maximumFractionDigits: nd });
const num = (x, nd = 2) => (x == null || isNaN(x)) ? "–" : Number(x).toLocaleString("en-US", { minimumFractionDigits: nd, maximumFractionDigits: nd });
const signed = (x, nd = 2) => (x == null || isNaN(x)) ? "–" : (x >= 0 ? "+" : "") + num(x, nd);
const clsSign = (x) => x > 0 ? "pos" : x < 0 ? "neg" : "";
const hhmmss = (t) => new Date(t * 1000).toLocaleTimeString("es", { hour12: false });

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Token": TOKEN }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* sin cuerpo */ }
  if (!r.ok && !data) throw new Error("HTTP " + r.status);
  return data;
}
let toastTimer;
function toast(msg, kind) {
  const t = $("toast"); t.textContent = msg; t.className = "toast " + (kind || ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 4500);
}
function ask({ title, text, input, okLabel }) {
  return new Promise((resolve) => {
    const dlg = $("dlg"), inp = $("dlgInput"), ok = $("dlgOk");
    $("dlgTitle").textContent = title; $("dlgText").textContent = text;
    inp.hidden = !input; inp.value = ""; inp.placeholder = input || "";
    ok.textContent = okLabel || "Confirmar";
    const check = () => { ok.disabled = !!input && inp.value.trim().toUpperCase() !== input; };
    inp.oninput = check; check();
    dlg.onclose = () => resolve(dlg.returnValue === "ok");
    dlg.returnValue = "cancel"; dlg.showModal();
  });
}

/* ------------------------------------------------------------------ pestañas */
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
  if (b.dataset.tab === "config" && !cfgLoaded) loadConfig();
  if (b.dataset.tab === "panel") chart.draw();
  if (b.dataset.tab === "historial" && S) renderFullHistory(S);
}));

/* ------------------------------------------------------------------ controles del gráfico e indicadores */
const indState = {
  ema20: true,
  ema50: true,
  sma200: true,
  bb: true,
  sr: true,
  fib: true,
  strat: true
};

document.querySelectorAll(".ind-pill").forEach((btn) => {
  btn.addEventListener("click", () => {
    const ind = btn.dataset.ind;
    if (ind && indState.hasOwnProperty(ind)) {
      indState[ind] = !indState[ind];
      btn.classList.toggle("on", indState[ind]);
      chart.draw();
    }
  });
});

document.querySelectorAll("#chartSizeSeg button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#chartSizeSeg button").forEach((b) => b.classList.toggle("on", b === btn));
    const sz = btn.dataset.size;
    const wrap = $("chartWrap");
    const sec = $("chartCardSection");
    if (!wrap) return;
    wrap.className = "chartwrap size-" + sz;
    if (sec) sec.classList.toggle("full-expanded", sz === "full" || sz === "lg");
    setTimeout(() => chart.draw(), 50);
  });
});

/* ------------------------------------------------------------------ gráfico de velas */
class Chart {
  constructor(canvas, tip) {
    this.cv = canvas; this.tip = tip; this.ctx = canvas.getContext("2d");
    this.d = null; this.n = 0; this.count = 120; this.offset = 0; this.mouse = null; this.drag = null; this.tf = null;
    new ResizeObserver(() => this.draw()).observe(canvas.parentElement);
    // Zoom solo con Ctrl/⌘ + rueda (o pellizco): la rueda sola sigue desplazando la página.
    canvas.addEventListener("wheel", (e) => { if (!(e.ctrlKey || e.metaKey)) return; e.preventDefault(); this.zoom(e); }, { passive: false });
    canvas.addEventListener("mousedown", (e) => { this.drag = { x: e.clientX, off: this.offset }; });
    window.addEventListener("mouseup", () => { this.drag = null; });
    canvas.addEventListener("mousemove", (e) => this.move(e));
    canvas.addEventListener("mouseleave", () => { this.mouse = null; this.tip.hidden = true; this.draw(); });
    canvas.addEventListener("dblclick", () => { this.count = Math.min(120, this.n); this.offset = 0; this.draw(); });
  }
  setData(d, resetView) {
    this.d = d; const s = d.series;
    if (!s) { this.n = 0; this.draw(); return; }
    const keys = ["open_time", "open", "high", "low", "close", "volume", "ema20", "ema50", "sma200", "bb_up", "bb_low"];
    const a = {}; keys.forEach((k) => (a[k] = s[k].slice()));
    const f = d.forming;
    if (f) { a.open_time.push(f.open_time); a.open.push(f.open); a.high.push(f.high); a.low.push(f.low); a.close.push(f.close); a.volume.push(f.volume);["ema20", "ema50", "sma200", "bb_up", "bb_low"].forEach((k) => a[k].push(null)); }
    this.a = a; this.n = a.open_time.length;
    if (resetView) { this.count = Math.min(120, this.n); this.offset = 0; }
    this.draw();
  }
  geo() {
    const r = this.cv.parentElement.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1;
    const W = Math.max(300, r.width), H = Math.max(240, r.height);
    if (this.cv.width !== Math.round(W * dpr) || this.cv.height !== Math.round(H * dpr)) { this.cv.width = Math.round(W * dpr); this.cv.height = Math.round(H * dpr); }
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { W, H, L: 6, R: 66, T: 8, B: 22 };
  }
  range() { const i1 = this.n - this.offset; const i0 = Math.max(0, i1 - this.count); return [i0, i1]; }
  zoom(e) { this.zoomBy(e.deltaY > 0 ? 1.15 : 1 / 1.15, e.offsetX); }
  zoomBy(factor, px) {
    if (!this.n) return; const g = this.geo(); const bw = (g.W - g.L - g.R) / this.count;
    const x = (px == null ? g.W - g.R : px) - g.L; const idxAt = this.range()[0] + x / bw;
    const nc = Math.round(this.count * factor);
    this.count = Math.max(20, Math.min(this.n, nc === this.count ? this.count + Math.sign(factor - 1) : nc));
    const i0 = idxAt - x / ((g.W - g.L - g.R) / this.count);
    this.offset = Math.max(0, Math.min(this.n - 10, Math.round(this.n - (i0 + this.count))));
    this.draw();
  }
  move(e) {
    const g = this.geo(); const bw = (g.W - g.L - g.R) / this.count;
    if (this.drag) { const db = Math.round((e.clientX - this.drag.x) / bw); this.offset = Math.max(0, Math.min(this.n - 10, this.drag.off + db)); }
    this.mouse = { x: e.offsetX, y: e.offsetY }; this.draw();
  }
  draw() {
    const ctx = this.ctx; const g = this.geo(); ctx.clearRect(0, 0, g.W, g.H);
    if (!this.n || !this.a) { ctx.fillStyle = css("--muted"); ctx.font = "14px sans-serif"; ctx.fillText("Cargando velas…", 20, 30); return; }
    const a = this.a; const [i0, i1] = this.range(); const vis = i1 - i0;
    const pw = g.W - g.L - g.R, ph = g.H - g.T - g.B, volH = ph * 0.16, priceH = ph - volH - 6;
    const bw = pw / this.count; const xOf = (i) => g.L + (i - i0 + 0.5) * bw;
    let lo = Infinity, hi = -Infinity, vmax = 0;
    for (let i = i0; i < i1; i++) { lo = Math.min(lo, a.low[i]); hi = Math.max(hi, a.high[i]); vmax = Math.max(vmax, a.volume[i]); }
    const ann = (this.d && this.d.annotations) || {}; const tr = ann.trade;
    if (tr) for (const k of ["sl", "tp", "entry"]) if (tr[k]) { lo = Math.min(lo, tr[k]); hi = Math.max(hi, tr[k]); }
    const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
    const yOf = (p) => g.T + (hi - p) / (hi - lo) * priceH;
    const text = css("--muted"), line = css("--line");
    ctx.font = "11px Consolas, monospace"; ctx.textBaseline = "middle";
    // rejilla de precio
    const step = niceStep((hi - lo) / 6);
    for (let p = Math.ceil(lo / step) * step; p < hi; p += step) {
      const y = yOf(p); ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(g.L, y); ctx.lineTo(g.W - g.R, y); ctx.stroke();
      ctx.fillStyle = text; ctx.textAlign = "left"; ctx.fillText(num(p, step < 1 ? 2 : 0), g.W - g.R + 6, y);
    }
    // eje de tiempo
    ctx.textAlign = "center"; ctx.textBaseline = "top"; const every = Math.max(1, Math.round(90 / bw));
    for (let i = i0; i < i1; i += every) {
      const dt = new Date(a.open_time[i]); const big = ["8h", "12h", "16h", "20h", "1d", "2d", "3d", "1w", "1M"].includes(this.tf);
      const lab = big ? dt.toLocaleDateString("es", { day: "2-digit", month: "short" }) : dt.toLocaleString("es", { day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
      ctx.fillStyle = text; ctx.fillText(lab, xOf(i), g.H - g.B + 5);
    }
    ctx.textBaseline = "middle";
    // zona Fibonacci
    if (ann.fib && indState.fib) {
      const y1 = yOf(ann.fib.zone_hi), y2 = yOf(ann.fib.zone_lo);
      ctx.fillStyle = css("--accent"); ctx.globalAlpha = 0.12; ctx.fillRect(g.L, y1, pw, y2 - y1); ctx.globalAlpha = 1;
      ctx.fillStyle = css("--accent"); ctx.textAlign = "left"; ctx.fillText("Fib 50–61.8%", g.L + 4, y1 + 8);
    }
    // soportes / resistencias (pivotes del ZigZag)
    if (indState.sr) {
      for (const sr of (ann.sr || [])) {
        if (sr.price < lo || sr.price > hi) continue;
        const y = yOf(sr.price), col = sr.kind === "S" ? css("--up") : css("--down");
        ctx.globalAlpha = 0.55; ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.setLineDash([1, 4]); ctx.beginPath(); ctx.moveTo(g.L, y); ctx.lineTo(g.W - g.R, y); ctx.stroke(); ctx.setLineDash([]);
        ctx.globalAlpha = 0.9; ctx.fillStyle = col; ctx.textAlign = "left"; ctx.fillText(sr.kind + " " + num(sr.price, 0), g.L + 4, y - 6); ctx.globalAlpha = 1;
      }
    }
    // niveles de la martingala
    if (indState.strat) {
      (ann.levels || []).forEach((lv, k) => {
        if (k === 0 || lv.price < lo || lv.price > hi) return;
        const y = yOf(lv.price), col = css("--warn");
        ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.setLineDash(lv.filled ? [] : [8, 4]); ctx.beginPath(); ctx.moveTo(g.L, y); ctx.lineTo(g.W - g.R, y); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = col; ctx.fillRect(g.W - g.R, y - 8, g.R - 2, 16); ctx.fillStyle = "#111"; ctx.textAlign = "left"; ctx.fillText("N" + (k + 1) + (lv.filled ? "✔" : "") + " " + num(lv.price, 0), g.W - g.R + 3, y);
      });
    }
    // bandas e indicadores
    const series = (arr, color, w, dash) => {
      ctx.strokeStyle = color; ctx.lineWidth = w; ctx.setLineDash(dash || []); ctx.beginPath(); let on = false;
      for (let i = i0; i < i1; i++) { const v = arr[i]; if (v == null) { on = false; continue; } const x = xOf(i), y = yOf(v); on ? ctx.lineTo(x, y) : ctx.moveTo(x, y); on = true; }
      ctx.stroke(); ctx.setLineDash([]);
    };
    if (indState.bb) { series(a.bb_up, css("--bb"), 1, [3, 3]); series(a.bb_low, css("--bb"), 1, [3, 3]); }
    if (indState.sma200) series(a.sma200, css("--sma200"), 1.4);
    if (indState.ema50) series(a.ema50, css("--ema50"), 1.2);
    if (indState.ema20) series(a.ema20, css("--ema20"), 1.2);
    // velas + volumen
    const up = css("--up"), dn = css("--down"), vu = css("--vol-up"), vd = css("--vol-down");
    const cw = Math.max(1, bw * 0.7);
    for (let i = i0; i < i1; i++) {
      const o = a.open[i], c = a.close[i], x = xOf(i), isUp = c >= o;
      ctx.strokeStyle = ctx.fillStyle = isUp ? up : dn; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, yOf(a.high[i])); ctx.lineTo(x, yOf(a.low[i])); ctx.stroke();
      const yo = yOf(o), yc = yOf(c); ctx.fillRect(x - cw / 2, Math.min(yo, yc), cw, Math.max(1, Math.abs(yo - yc)));
      ctx.fillStyle = isUp ? vu : vd; const vh = vmax ? a.volume[i] / vmax * volH : 0; ctx.fillRect(x - cw / 2, g.T + ph - vh, cw, vh);
    }
    // marcadores de operaciones en el gráfico
    if (indState.strat) {
      for (const hst of (ann.history_markers || (this.d && this.d.history) || [])) {
        for (let i = i0; i < i1; i++) {
          const tClose = hst.close_time || hst.time;
          if (tClose >= a.open_time[i] && (i === this.n - 1 || tClose < a.open_time[i + 1])) {
            const isWin = (hst.net || 0) >= 0;
            const x = xOf(i), y = yOf(hst.exit || a.close[i]);
            ctx.fillStyle = isWin ? up : dn; ctx.beginPath();
            ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
            ctx.strokeStyle = "#fff"; ctx.lineWidth = 1; ctx.stroke();
            break;
          }
        }
      }
    }
    // líneas de la operación abierta o niveles proyectados de la estrategia
    if (tr && indState.strat) {
      const hl = (p, col, lab, sub, dash) => {
        if (!p || p < lo || p > hi) return; const y = yOf(p); ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.setLineDash(dash || []); ctx.beginPath(); ctx.moveTo(g.L, y); ctx.lineTo(g.W - g.R, y); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = col; ctx.fillRect(g.W - g.R - 2, y - 9, g.R + 4, 18);
        ctx.fillStyle = "#111"; ctx.font = "bold 9.5px Consolas, monospace"; ctx.textAlign = "left"; ctx.fillText(lab + " " + num(p, 0), g.W - g.R + 2, y - 1);
        if (sub) { ctx.font = "8.5px Consolas, monospace"; ctx.fillText(sub, g.W - g.R + 2, y + 7); }
      };
      hl(tr.entry, css("--accent"), "ENTRADA", tr.margin ? "$" + num(tr.margin, 0) + " M" : "", [4, 4]);
      hl(tr.sl, dn, tr.be_done ? "BE" : "STOP LOSS", tr.loss_sl_usdt ? "-$" + num(tr.loss_sl_usdt, 2) : "", [4, 4]);
      hl(tr.tp, up, "TAKE PROFIT", tr.gain_tp_usdt ? "+$" + num(tr.gain_tp_usdt, 2) : "", [4, 4]);
      if (tr.liq_price) hl(tr.liq_price, css("--warn"), "LIQ", "", [2, 4]);
    } else if (ann.strategy_levels && indState.strat) {
      const st = ann.strategy_levels;
      const hl = (p, col, lab, sub, dash) => {
        if (!p || p < lo || p > hi) return; const y = yOf(p); ctx.strokeStyle = col; ctx.lineWidth = 1.3; ctx.setLineDash(dash || []); ctx.beginPath(); ctx.moveTo(g.L, y); ctx.lineTo(g.W - g.R, y); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = col; ctx.fillRect(g.W - g.R - 2, y - 9, g.R + 4, 18);
        ctx.fillStyle = "#111"; ctx.font = "bold 9.5px Consolas, monospace"; ctx.textAlign = "left"; ctx.fillText(lab + " " + num(p, 0), g.W - g.R + 2, y - 1);
        if (sub) { ctx.font = "8.5px Consolas, monospace"; ctx.fillText(sub, g.W - g.R + 2, y + 7); }
      };
      hl(st.target_price, "#29b6f6", "EJECUCIÓN", "Objetivo", [4, 4]);
      hl(st.sl_price, "rgba(239,83,80,0.85)", "SL Est.", "-$" + num(st.loss_sl_usdt, 2), [3, 4]);
      hl(st.tp_price, "rgba(38,166,154,0.85)", "TP Est.", "+$" + num(st.gain_tp_usdt, 2), [3, 4]);
    }
    // último precio
    const last = a.close[this.n - 1];
    if (this.offset === 0 && last >= lo && last <= hi) {
      const y = yOf(last), col = last >= a.open[this.n - 1] ? up : dn;
      ctx.strokeStyle = col; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(g.L, y); ctx.lineTo(g.W - g.R, y); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = col; ctx.fillRect(g.W - g.R, y - 8, g.R - 2, 16); ctx.fillStyle = "#fff"; ctx.textAlign = "left"; ctx.fillText(num(last, 1), g.W - g.R + 3, y);
    }
    // cursor y tooltip
    const m = this.mouse;
    if (m && m.x > g.L && m.x < g.W - g.R && m.y > g.T && m.y < g.H - g.B) {
      ctx.strokeStyle = text; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(m.x, g.T); ctx.lineTo(m.x, g.H - g.B); ctx.moveTo(g.L, m.y); ctx.lineTo(g.W - g.R, m.y); ctx.stroke(); ctx.setLineDash([]);
      const i = Math.min(i1 - 1, Math.max(i0, Math.floor(i0 + (m.x - g.L) / bw)));
      const f = (v, nd) => v == null ? "–" : num(v, nd === undefined ? 1 : nd);
      const dt = new Date(a.open_time[i]);
      this.tip.textContent = `${dt.toLocaleString("es", { hour12: false })}  (UTC ${dt.toISOString().slice(5, 16).replace("T", " ")})\nO ${f(a.open[i])}  H ${f(a.high[i])}\nL ${f(a.low[i])}  C ${f(a.close[i])}\nVol ${f(a.volume[i], 1)}\nEMA20 ${f(a.ema20[i])}  EMA50 ${f(a.ema50[i])}\nSMA200 ${f(a.sma200[i])}\nBB ${f(a.bb_low[i])} – ${f(a.bb_up[i])}`;
      this.tip.hidden = false; this.tip.style.left = Math.min(m.x + 14, g.W - 250) + "px"; this.tip.style.top = Math.max(4, m.y - 90) + "px";
    } else this.tip.hidden = true;
  }
}
function niceStep(raw) { const p = Math.pow(10, Math.floor(Math.log10(raw))); const f = raw / p; return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p; }
const chart = new Chart($("chart"), $("tip"));
$("zoomIn").onclick = () => chart.zoomBy(1 / 1.3); $("zoomOut").onclick = () => chart.zoomBy(1.3);
let chartTf = null, lastCandleTf = null;

async function refreshChart() {
  try {
    const tf = chartTf || (window.__triggerTf || "1h");
    const d = await api("/api/candles?tf=" + tf + "&n=400");
    if (!d) return;
    const sel = $("tfSelect");
    if (!sel.options.length) { CHART_TFS.forEach((t) => sel.append(h("option", { value: t, text: t }))); sel.onchange = () => { chartTf = sel.value; refreshChart(); }; }
    if (document.activeElement !== sel) sel.value = d.tf || tf;
    chart.tf = d.tf; const reset = lastCandleTf !== d.tf; lastCandleTf = d.tf; chart.setData(d, reset);
  } catch (e) { /* reintenta en el siguiente ciclo */ }
}

/* ------------------------------------------------------------------ render del estado */
let lastLogId = 0, S = null;
function chip(el, text, kind, title) { el.textContent = text; el.className = "chip " + (kind || ""); el.title = title || el.title; }

function renderChecks(tbody, checks, withSides) {
  tbody.replaceChildren();
  if (!checks || !checks.length) { tbody.append(h("tr", {}, h("td", { colspan: withSides ? 4 : 2, class: "val", text: "Pendiente de la fase anterior." }))); return; }
  const ic = (v) => h("td", { class: "ic " + (v === true ? "t" : v === false ? "f" : "n"), text: v === true ? "✔" : v === false ? "✘" : "–" });
  for (const c of checks) {
    const tr = h("tr");
    if (withSides) tr.append(h("td", { text: c.name }), h("td", { class: "val", text: c.value }), ic(c.long), ic(c.short));
    else tr.append(ic(c.ok), h("td", {}, h("div", { text: c.name }), h("div", { class: "val", text: c.value })));
    tbody.append(tr);
  }
}
function badge(el, ok) { el.textContent = ok === true ? "cumplida" : ok === false ? "no cumplida" : "–"; el.className = "mini " + (ok === true ? "t" : "f"); }
function kv(box, rows) {
  box.replaceChildren();
  for (const [k, v, cls] of rows) { box.append(h("div", { text: k }), h("div", { class: cls || "", text: v })); }
}

function render(s) {
  S = s; window.__triggerTf = s.trigger_tf;
  const r = s.tf_roles;
  $("ph1Title").textContent = "Fase 1 · Tendencia (" + [r.tendencia, r.superior].filter(Boolean).join(" + ") + ")";
  $("tfRoles").textContent = "Tendencia " + r.tendencia + (r.superior ? " + " + r.superior : "") + " · Retrocesos " + r.zona + " · Fibonacci " + r.fibonacci + " · Gatillo " + r.gatillo;
  chip($("chipMode"), s.mode === "REAL" ? "CUENTA REAL" : "TESTNET", s.mode === "REAL" ? "real" : "info");
  const st = s.fatal ? ["ERROR DE CONFIG", "bad"] : !s.booted ? ["Iniciando…", "warn"] : s.paused ? ["EN PAUSA", "warn"] : s.trading_ready ? ["OPERANDO", "ok"] : ["SOLO MONITOREO", "info"];
  chip($("chipStatus"), st[0], st[1], s.paused ? s.pause_reason : s.trading_note);
  chip($("chipFeed"), s.feed.ws ? "Datos ● en vivo" : "Datos ○ reconectando", s.feed.ws ? "ok" : "bad", "WebSocket de velas de Binance");
  chip($("chipApi"), s.api.ok === true ? "API ✔" : s.api.ok === false ? "API ✘" : "API –", s.api.ok === true ? "ok" : s.api.ok === false ? "bad" : "", s.api.error || "Conexión con la API de Binance");
  const tg = s.telegram; chip($("chipTg"), tg.linked ? (tg.ok === false ? "Telegram ✘" : "Telegram ✔") : tg.token ? "Telegram sin vincular" : "Telegram off", tg.linked ? (tg.ok === false ? "bad" : "ok") : tg.token ? "warn" : "");
  $("price").textContent = s.price ? usd(s.price) : "–"; $("priceAge").textContent = s.price_age != null ? "hace " + Math.round(s.price_age) + " s" : "";
  $("btnPause").hidden = s.paused; $("btnResume").hidden = !s.paused;

  // resumen financiero en encabezado
  const fin = s.financials || {};
  const walletAmt = s.balance && s.balance.wallet != null ? s.balance.wallet : (fin.capital_config || 100);
  const topW = $("topWallet"); if (topW) topW.textContent = usd(walletAmt, 2);

  const topM = $("topMarginInUse");
  const topSL = $("topSlRisk");
  const topTP = $("topTpTarget");
  const mb = $("mBadgeInPlay");

  if (s.trade) {
    const tr = s.trade;
    if (topM) topM.textContent = usd(tr.margin || fin.margin_config || 20, 2);
    const slLoss = tr.loss_sl_usdt != null ? tr.loss_sl_usdt : (fin.risk_config || 3.0);
    const tpGain = tr.gain_tp_usdt != null ? tr.gain_tp_usdt : (slLoss * (fin.ratio_config || 3.0));
    if (topSL) {
      topSL.textContent = "–" + usd(slLoss, 2);
      topSL.title = "SL en " + (tr.sl ? usd(tr.sl, 1) : "–") + (tr.be_done ? " (Break-even activo)" : "");
    }
    if (topTP) {
      topTP.textContent = "+" + usd(tpGain, 2);
      topTP.title = "TP en " + (tr.tp ? usd(tr.tp, 1) : "–");
    }
    if (mb) mb.classList.add("highlight");
  } else {
    if (topM) topM.textContent = "$0.00";
    const slPlan = fin.risk_config || 3.0;
    const tpPlan = slPlan * (fin.ratio_config || 3.0);
    if (topSL) {
      topSL.textContent = "–" + usd(slPlan, 2);
      topSL.title = "Riesgo presupuestado ante Stop Loss";
    }
    if (topTP) {
      topTP.textContent = "+" + usd(tpPlan, 2);
      topTP.title = "Ganancia objetivo presupuestada ante Take Profit (Ratio 1:3)";
    }
    if (mb) mb.classList.remove("highlight");
  }

  const al = $("alerts"); al.replaceChildren();
  if (s.fatal) al.append(h("div", { class: "alert error", text: s.fatal }));
  for (const a of s.alerts) al.append(h("div", { class: "alert " + a.level, text: a.text }));
  if (s.paused && s.pause_reason && !s.fatal) al.append(h("div", { class: "alert info", text: "Bot en pausa: " + s.pause_reason + ". No abrirá nuevas operaciones." }));

  // análisis
  const ev = s.evaluation || {}; const t = ev.trend || "SIN_DATOS";
  const tmap = { LONG: ["ALCISTA → solo LONG", "ok"], SHORT: ["BAJISTA → solo SHORT", "bad"], STANDBY: ["STANDBY · mercado lateral", "info"], NONE: ["Sin tendencia clara", "warn"], SIN_DATOS: ["Sin datos", ""] };
  chip($("trendBadge"), tmap[t][0], tmap[t][1]); $("evNote").textContent = ev.note || "";
  renderChecks($("ph1").querySelector("tbody"), ev.phase1, true); renderChecks($("ph2").querySelector("tbody"), ev.phase2, false); renderChecks($("ph3").querySelector("tbody"), ev.phase3, false);
  badge($("ph2Badge"), ev.phase2_ok === undefined ? null : ev.phase2_ok); badge($("ph3Badge"), ev.phase3_ok === undefined ? null : ev.phase3_ok);

  // estrategia y puntos de ejecución
  if (s.strategy_info) {
    const sb = $("stratBadge"), st = $("stratTitle"), sd = $("stratDesc");
    if (sb) sb.textContent = s.strategy_info.name || "Estrategia 1";
    if (st) st.textContent = (s.strategy_info.name || "Estrategia") + " · " + (s.strategy_info.description || "");
    if (sd) sd.textContent = s.strategy_info.long_description || s.strategy_info.description || "";
  }
  const topSel = $("selStrategyTop");
  if (topSel && s.strategy && topSel.value !== s.strategy && topSel.options.length) {
    topSel.value = s.strategy;
  }
  const ep = s.execution_points || (s.evaluation && s.evaluation.execution_points);
  if (ep) {
    const epP = $("epPrice"), epTP = $("epTargetPrice"), epR = $("epRsi"), epTR = $("epTargetRsi"), epMsg = $("epStatusMsg");
    if (epP) epP.textContent = ep.current_price ? usd(ep.current_price, 1) : "–";
    if (epTP) epTP.textContent = ep.target_price ? usd(ep.target_price, 1) : "–";
    if (epR) epR.textContent = ep.current_rsi != null ? num(ep.current_rsi, 1) : "–";
    if (epTR) epTR.textContent = ep.target_rsi != null ? (ep.side === "LONG" ? "< " : "> ") + ep.target_rsi : "–";
    if (epMsg) {
      epMsg.textContent = ep.message || "Evaluando condiciones…";
      epMsg.className = "exec-msg" + (ep.target_reached ? " reached" : "");
    }
  }

  // posición
  const pb = $("posBox"); const tr = s.trade; $("btnClose").hidden = !(tr || s.position);
  if (tr) {
    pb.className = "kv";
    kv(pb, [["Lado", tr.side, tr.side === "LONG" ? "pos" : "neg"], ["Entrada", usd(tr.entry)], ["Cantidad", tr.qty + " BTC"], ["Marca", usd(tr.mark)],
      ["PnL no realizado", signed(tr.upnl) + " USDT (" + signed(tr.r_now) + " R)", clsSign(tr.upnl)], ["Stop Loss", usd(tr.sl) + (tr.be_done ? " · break-even" : "")], ["Take Profit", usd(tr.tp)],
      ["Liquidación", usd(tr.liq_price)], ["Margen", usd(tr.margin) + " · " + tr.leverage + "x"], ["Patrón", tr.pattern || "–"],
      ...(tr.martingale && tr.levels ? [["Niveles", tr.levels.map((l) => l.qty + " @ " + num(l.price, 0) + (l.filled ? " ✔" : "")).join(" · ")]] : [])]);
  } else if (s.position) {
    pb.className = "kv"; kv(pb, [["Posición ajena al bot", num(s.position.amt, 4) + " BTC"], ["Entrada", usd(s.position.entry)], ["PnL no realizado", signed(s.position.upnl), clsSign(s.position.upnl)]]);
  } else { pb.className = "muted"; pb.textContent = "Sin operación abierta."; }
  const b = s.balance; kv($("balBox"), b ? [["Saldo total", usd(b.wallet) + " USDT"], ["Disponible", usd(b.available) + " USDT"]] : [["Saldo", s.trading_ready ? "…" : "sin conexión de trading"]]);
  const tb = $("histTable").querySelector("tbody"); tb.replaceChildren();
  for (const x of [...s.history].reverse().slice(0, 10)) {
    tb.append(h("tr", {},
      h("td", { text: new Date(x.close_time).toLocaleString("es", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) }),
      h("td", { text: x.side }),
      h("td", { text: usd(x.entry, 1) }),
      h("td", { text: usd(x.exit, 1) }),
      h("td", { class: clsSign(x.net), text: signed(x.net) + " USDT" }),
      h("td", { text: x.reason })
    ));
  }
  if (!s.history.length) tb.append(h("tr", {}, h("td", { colspan: 6, class: "val", text: "Aún no hay operaciones en vivo cerradas." })));
  $("board").textContent = s.board || "Calculando…";

  // logs
  const lb = $("logbox"); const nearEnd = lb.scrollHeight - lb.scrollTop - lb.clientHeight < 60;
  for (const l of s.logs) {
    if (l.id <= lastLogId) continue; lastLogId = l.id;
    const line = h("div", { class: "logline lv-" + l.level }); line.append(h("span", { class: "t", text: hhmmss(l.t) + "  " }), document.createTextNode(l.msg)); lb.append(line);
  }
  while (lb.childElementCount > 500) lb.firstChild.remove();
  if (nearEnd) lb.scrollTop = lb.scrollHeight;

  // configuración (partes vivas)
  $("ipBox").textContent = s.public_ip || "–";
  renderTelegram(s); renderBacktest(s);
  renderFullHistory(s);
}

/* ------------------------------------------------------------------ pestaña historial completo */
let histFilter = "all";
document.querySelectorAll("#histFilterSeg button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#histFilterSeg button").forEach((x) => x.classList.toggle("on", x === b));
  histFilter = b.dataset.filter || "all";
  if (S) renderFullHistory(S);
}));

function renderFullHistory(s) {
  if (!s) return;
  const all = [...(s.history || [])].reverse();
  const filtered = all.filter((x) => {
    if (histFilter === "win") return (x.net || 0) > 0;
    if (histFilter === "loss") return (x.net || 0) < 0;
    return true;
  });

  const total = all.length;
  const wins = all.filter((x) => (x.net || 0) > 0);
  const losses = all.filter((x) => (x.net || 0) < 0);
  const winCount = wins.length;
  const winrate = total ? (winCount / total * 100).toFixed(1) : "0.0";
  const netSum = all.reduce((acc, x) => acc + (x.net || 0), 0);
  const winSum = wins.reduce((acc, x) => acc + (x.net || 0), 0);
  const lossSum = Math.abs(losses.reduce((acc, x) => acc + (x.net || 0), 0));
  const avgWin = wins.length ? (winSum / wins.length).toFixed(2) : "0.00";
  const avgLoss = losses.length ? (lossSum / losses.length).toFixed(2) : "0.00";
  const pf = lossSum > 0 ? (winSum / lossSum).toFixed(2) : (winSum > 0 ? "∞" : "0.00");

  const hmC = $("hmCount"), hmW = $("hmWinrate"), hmN = $("hmNet");
  const hmAW = $("hmAvgWin"), hmAL = $("hmAvgLoss"), hmPF = $("hmProfitFactor");
  if (hmC) hmC.textContent = total;
  if (hmW) hmW.textContent = winrate + "%";
  if (hmN) { hmN.textContent = signed(netSum, 2) + " USDT"; hmN.className = clsSign(netSum); }
  if (hmAW) hmAW.textContent = "+" + usd(avgWin, 2);
  if (hmAL) hmAL.textContent = "-" + usd(avgLoss, 2);
  if (hmPF) hmPF.textContent = pf;

  const fullTable = $("fullHistTable");
  if (fullTable) {
    const tb = fullTable.querySelector("tbody");
    tb.replaceChildren();
    for (const x of filtered) {
      const dt = new Date(x.close_time || x.entry_time || Date.now());
      tb.append(h("tr", {},
        h("td", { text: dt.toLocaleString("es", { hour12: false }) }),
        h("td", { text: x.side || "–" }),
        h("td", { text: x.pattern || "Estrategia 1" }),
        h("td", { text: usd(x.entry, 1) }),
        h("td", { text: usd(x.exit, 1) }),
        h("td", { text: x.margin ? usd(x.margin, 1) : "–" }),
        h("td", { class: clsSign(x.net), text: signed(x.net, 2) + " USDT" }),
        h("td", { class: clsSign(x.r), text: signed(x.r, 2) + " R" }),
        h("td", { text: x.reason || "–" })
      ));
    }
  }
  const empty = $("fullHistEmpty");
  if (empty) empty.hidden = total > 0;
}

let polling = false;
async function poll() {
  if (polling) return; polling = true;
  try { const s = await api("/api/state?since=" + lastLogId); if (s) render(s); } catch (e) { chip($("chipFeed"), "Dashboard sin conexión", "bad"); }
  polling = false;
}

/* ------------------------------------------------------------------ acciones de cabecera */
$("btnPause").onclick = async () => { await api("/api/pause", {}); poll(); };
$("btnResume").onclick = async () => {
  const r = await api("/api/resume", {}); if (r && r.ok === false) toast(r.error, "err"); poll();
};
$("btnClose").onclick = async () => {
  if (await ask({ title: "Cerrar posición", text: "Se cerrará a mercado la posición abierta. ¿Continuar?", okLabel: "Cerrar ahora" })) {
    const r = await api("/api/close_position", {}); toast(r.message, "ok"); poll();
  }
};

/* ------------------------------------------------------------------ configuración */
const TREND = ["4h", "8h", "12h", "16h", "20h", "1d", "2d", "3d", "1w", "1M"];
const PULL = ["5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"];
const TRIG = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"];
const CHART_TFS = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "16h", "20h", "1d", "2d", "3d", "1w", "1M"];
const TFS = [
  ["TF_TENDENCIA", "Tendencia: ¿alcista o bajista?", TREND, "Temporalidad que decide si solo se compra o solo se vende."],
  ["TF_TENDENCIA_SUPERIOR", "Filtro de tendencia superior", [["", "(ninguno)"], ...TREND], "Opcional: una temporalidad mayor que también debe acompañar."],
  ["TF_ZONA", "Retrocesos: zona EMA20/EMA50", PULL, "Aquí se mide si el precio retrocede a la zona de valor."],
  ["TF_FIBONACCI", "Fibonacci y soportes/resistencias", PULL, "Aquí se detecta el último impulso y los pivotes."],
  ["TF_GATILLO", "Acción: gatillo de entrada", TRIG, "Patrón de velas + agotamiento; de aquí salen el ATR y el Stop Loss."],
];
const MART = [
  ["MARTINGALA_ACTIVA", "Martingala activada", "bool", "Desactivada por defecto."],
  ["MARTINGALA_NIVELES", "Entradas totales (niveles)", 1, "De 2 a 4. Si no caben, se usan menos."],
  ["MARTINGALA_MULTIPLICADOR", "Multiplicador por nivel", 0.5, "2 = 1x, 2x, 4x…"],
  ["MARTINGALA_FUENTE", "Dónde van los niveles", [["fibonacci", "Fibonacci (50 / 61.8 / 78.6%)"], ["soportes", "Soportes/resistencias"]], "Si falta alguno se completa con la otra fuente o con pasos de ATR."],
  ["MARTINGALA_RIESGO_MAX_USDT", "Riesgo máx. con martingala (USDT)", 0.5, "0 = el riesgo máximo normal. Súbelo para permitir más niveles."],
];
const RISK = [
  ["CAPITAL_TOTAL_CUENTA", "Capital total (USDT)", 1, "Saldo total de tu cuenta de Futuros."],
  ["MARGEN_EN_USO_POR_OPERACION", "Margen por operación (USDT)", 1, "Dinero propio que se bloquea como garantía."],
  ["APALANCAMIENTO", "Apalancamiento (x)", 1, "Margen aislado."],
  ["RIESGO_MAXIMO_PERMITIDO_USDT", "Riesgo máximo por operación (USDT)", 0.5, "Pérdida real máxima si salta el Stop Loss."],
  ["RATIO_RIESGO_BENEFICIO", "Ratio riesgo:beneficio", 0.5, "TP = N × distancia del SL."],
  ["ATR_MULT_SL", "Multiplicador ATR del Stop Loss", 0.1, "SL = extremo de la vela gatillo ∓ N × ATR(14)."],
  ["TASA_COMISION_TAKER", "Comisión taker", 0.0001, "0.0005 = 0.05% por orden a mercado."],
  ["TASA_MANTENIMIENTO_BTC", "Margen de mantenimiento", 0.001, "0.004 = 0.4% (BTC)."],
  ["ACTIVAR_BREAK_EVEN", "Break-even automático", "bool", "Mueve el SL a la entrada al llegar a 1:1."],
];
const ADV = [
  ["ADX_TENDENCIA_MIN", "ADX mínimo (1d)", 1, "Tendencia confirmada si ADX > este valor."],
  ["ADX_LATERAL_MAX", "ADX lateral (STANDBY)", 1, "Por debajo de esto el bot no opera."],
  ["ZONA_TOLERANCIA_ATR", "Tolerancia a la zona EMA (× ATR)", 0.1, ""],
  ["FIB_MIN", "Fibonacci mínimo", 0.01, ""], ["FIB_MAX", "Fibonacci máximo", 0.001, ""],
  ["FIB_ZIGZAG_ATR", "ZigZag: reversión (× ATR 1d)", 0.1, "Para detectar el último impulso."],
  ["FIB_IMPULSO_MIN_ATR", "Impulso mínimo (× ATR 1d)", 0.5, "Tamaño mínimo para considerarlo «mayor»."],
  ["LONG_RSI_DIARIO_MIN", "LONG · RSI diario mín.", 1, ""], ["LONG_RSI_DIARIO_MAX", "LONG · RSI diario máx.", 1, ""],
  ["SHORT_RSI_DIARIO_MIN", "SHORT · RSI diario mín.", 1, ""], ["SHORT_RSI_DIARIO_MAX", "SHORT · RSI diario máx.", 1, ""],
  ["BB_PERIODO", "Bollinger · período", 1, ""], ["BB_DESVIACION", "Bollinger · desviaciones", 0.1, ""],
  ["RSI_SOBREVENTA", "RSI sobreventa (gatillo)", 1, ""], ["RSI_SOBRECOMPRA", "RSI sobrecompra (gatillo)", 1, ""],
  ["STOCH_SOBREVENTA", "Estocástico %K sobreventa", 1, ""], ["STOCH_SOBRECOMPRA", "Estocástico %K sobrecompra", 1, ""],
  ["VENTANA_AGOTAMIENTO", "Ventana de agotamiento (velas)", 1, "1 = estricto: debe darse en la vela gatillo."],
  ["PIN_MECHA_CUERPO", "Pin Bar · mecha/cuerpo", 0.1, ""], ["PIN_MECHA_OPUESTA_MAX", "Pin Bar · mecha opuesta máx.", 0.05, ""],
  ["ENGULFING_VOL_MULT", "Envolvente · volumen × SMA20", 0.1, ""],
  ["WORKING_TYPE", "Precio que dispara SL/TP", ["MARK_PRICE", "CONTRACT_PRICE"], "Precio de marca (recomendado) o último precio."],
  ["BREAK_EVEN_BUFFER_PCT", "Break-even · colchón", 0.0005, "0.001 = entrada +0.1%."],
  ["MAX_RETRASO_SENAL_SEG", "Retraso máx. de la señal (s)", 10, "Descarta señales viejas."],
];
let cfgLoaded = false, cfgValues = {}, modeSel = "testnet";

function buildForm(box, fields) {
  box.replaceChildren();
  for (const [key, label, kind, help] of fields) {
    const f = h("div", { class: "field" }); f.append(h("label", { for: "f_" + key, text: label }));
    let inp;
    if (kind === "bool") { inp = h("input", { type: "checkbox", id: "f_" + key }); const w = h("label", { class: "check" }, inp, " Activado"); f.append(w); }
    else if (Array.isArray(kind)) { inp = h("select", { id: "f_" + key }); kind.forEach((o) => { const [v, l] = Array.isArray(o) ? o : [o, o]; inp.append(h("option", { value: v, text: l })); }); f.append(inp); }
    else { inp = h("input", { type: "number", id: "f_" + key, step: String(kind) }); f.append(inp); }
    if (help) f.append(h("small", { class: "muted", text: help }));
    box.append(f);
  }
}
function fillForm(values) {
  for (const [key, , kind] of [...RISK, ...TFS, ...MART, ...ADV]) {
    const el = $("f_" + key); if (!el) continue;
    if (kind === "bool") el.checked = !!values[key]; else el.value = values[key];
  }
  $("TELEGRAM_ACTIVO").checked = !!values.TELEGRAM_ACTIVO; $("TELEGRAM_AVISAR_SENALES_BLOQUEADAS").checked = !!values.TELEGRAM_AVISAR_SENALES_BLOQUEADAS;
  $("testUrl").value = values.ENTORNO_PRUEBAS_URL; setMode(values.SANDBOX_MODE ? "testnet" : "real");
}
function setCredsTab(kind) {
  const isTest = kind === "testnet";
  const btnT = $("btnTabTestnet"), btnR = $("btnTabReal");
  const fsT = $("fsTestnet"), fsR = $("fsReal");
  if (btnT && btnR && fsT && fsR) {
    btnT.classList.toggle("on", isTest);
    btnR.classList.toggle("on", !isTest);
    fsT.hidden = !isTest;
    fsR.hidden = isTest;
  }
}
if ($("btnTabTestnet")) $("btnTabTestnet").onclick = () => setCredsTab("testnet");
if ($("btnTabReal")) $("btnTabReal").onclick = () => setCredsTab("real");

function setMode(m) { 
  modeSel = m; 
  document.querySelectorAll("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === m)); 
  setCredsTab(m);
}
function credStatus(id, info, showMask) { $(id).textContent = info && info.set ? "✔ guardada" + (showMask && info.mask ? " (" + info.mask + ")" : "") + " · escribe aquí para reemplazarla" : "no configurada"; }

async function loadConfig() {
  const c = await api("/api/config"); if (!c) return; cfgValues = c.values;
  buildForm($("formRisk"), RISK); buildForm($("formTfs"), TFS); buildForm($("formMart"), MART); buildForm($("formAdv"), ADV); fillForm(c.values); cfgLoaded = true;
  const cr = c.credentials;
  credStatus("st_testnet_api_key", cr.testnet_api_key, true); credStatus("st_testnet_api_secret", cr.testnet_api_secret);
  credStatus("st_real_api_key", cr.real_api_key, true); credStatus("st_real_api_secret", cr.real_api_secret); credStatus("st_telegram_token", cr.telegram_token);
  $("backendNote").textContent = "Las claves se guardan cifradas en: " + c.backend + " (nunca dentro de la carpeta del bot).";

  // opciones de estrategias
  const topSel = $("selStrategyTop");
  const btSel = $("btStrat");
  if (c.strategies) {
    if (topSel && !topSel.options.length) {
      for (const [id, info] of Object.entries(c.strategies)) {
        const descText = info.desc || info.description || "";
        topSel.append(h("option", { value: id, text: info.name + (descText ? " — " + descText : "") }));
      }
      topSel.onchange = async () => {
        await applyConfig({ ESTRATEGIA: topSel.value });
      };
    }
    if (topSel && c.active_strategy) topSel.value = c.active_strategy;
    if (btSel && !btSel.options.length) {
      for (const [id, info] of Object.entries(c.strategies)) {
        const descText = info.desc || info.description || "";
        btSel.append(h("option", { value: id, text: info.name + (descText ? " — " + descText : "") }));
      }
      if (c.active_strategy) btSel.value = c.active_strategy;
    }
  }
}

async function applyConfig(values, confirmReal) {
  const r = await api("/api/config", { values, confirm_real: confirmReal || "" });
  if (r && r.ok) { toast("Configuración guardada" + ((r.warnings || []).length ? " · " + r.warnings.join(" ") : ""), "ok"); await loadConfig(); poll(); }
  else toast(((r && r.errors) || ["Error"]).join(" · "), "err");
  return r;
}
document.querySelectorAll("#modeSeg button").forEach((b) => b.addEventListener("click", async () => {
  const m = b.dataset.mode; if (m === modeSel) return;
  if (m === "real") {
    const ok = await ask({ title: "Pasar a la cuenta REAL", text: "El bot operará con dinero real. Antes prueba la conexión real y revisa el tablero de riesgo. Escribe OPERAR REAL para confirmar. El bot quedará en pausa hasta que lo reanudes.", input: "OPERAR REAL", okLabel: "Activar cuenta real" });
    if (!ok) return;
    await applyConfig({ SANDBOX_MODE: false }, "OPERAR REAL");
  } else await applyConfig({ SANDBOX_MODE: true });
}));
$("testUrl").onchange = () => applyConfig({ ENTORNO_PRUEBAS_URL: $("testUrl").value });

$("btnSaveCfg").onclick = async () => {
  const values = {};
  for (const [key, , kind] of [...RISK, ...TFS, ...MART, ...ADV]) { const el = $("f_" + key); values[key] = kind === "bool" ? el.checked : el.value; }
  values.TELEGRAM_ACTIVO = $("TELEGRAM_ACTIVO").checked; values.TELEGRAM_AVISAR_SENALES_BLOQUEADAS = $("TELEGRAM_AVISAR_SENALES_BLOQUEADAS").checked;
  const r = await applyConfig(values); $("cfgMsg").textContent = r && r.ok ? "Guardado ✔" : "";
};
const CRED_IDS = ["testnet_api_key", "testnet_api_secret", "real_api_key", "real_api_secret", "telegram_token"];
// Guarda los campos escritos (los vacíos no se tocan). Devuelve {ok, saved, errors}.
async function saveTyped(ids) {
  const payload = {}; ids.forEach((i) => { const v = $(i).value.trim(); if (v) payload[i] = v; });
  if (!Object.keys(payload).length) return { ok: true, saved: false };
  const r = await api("/api/credentials", payload);
  if (r && r.ok) { ids.forEach((i) => ($(i).value = "")); await loadConfig(); return { ok: true, saved: true }; }
  return { ok: false, errors: (r && r.errors) || ["Error"] };
}
$("btnSaveTfs").onclick = () => $("btnSaveCfg").onclick();
$("btnSaveMart").onclick = () => $("btnSaveCfg").onclick();
$("btnSaveCreds").onclick = async () => {
  const s = await saveTyped(CRED_IDS);
  if (!s.ok) { toast(s.errors.join(" · "), "err"); return; }
  if (!s.saved) { toast("No hay nada nuevo que guardar.", "err"); return; }
  toast("Claves guardadas de forma segura.", "ok"); $("credsMsg").textContent = "Guardado ✔"; poll();
};

function showTest(el, r) {
  el.replaceChildren();
  const line = (txt, cls) => el.append(h("div", { class: cls || "", text: txt }));
  if (!r.ok) {
    line("✘ " + (r.error || "No se pudo conectar") + (r.code ? " [" + r.code + "]" : ""), "bad");
    if (r.auth) line("Revisa que la clave esté completa, tenga permiso de Futuros y que la IP de este PC (" + (r.public_ip || "?") + ") esté en la whitelist de la API key.", "warnc");
    return;
  }
  line("✔ Conexión correcta con " + r.url.replace("https://", ""), "good");
  line("Saldo: " + usd(r.wallet) + " USDT · disponible " + usd(r.available) + " USDT");
  if (r.hedge_mode) line("⚠ La cuenta está en Hedge Mode. Cámbiala a One-way Mode.", "bad");
  if (r.trade_ok === true) line("✔ Permiso de trading/Futuros verificado", "good"); else if (r.position_open) line("(No se verificó el permiso de trading porque hay una posición abierta)", "warnc");
  if (r.restrictions) {
    line(r.restrictions.futures ? "✔ Futuros habilitado en la clave" : "✘ Futuros NO habilitado en la clave", r.restrictions.futures ? "good" : "bad");
    line(r.restrictions.withdrawals ? "⚠ Los RETIROS están habilitados: desactívalos en Binance." : "✔ Retiros deshabilitados", r.restrictions.withdrawals ? "bad" : "good");
    line(r.restrictions.ip_restrict ? "✔ Clave restringida a IPs de confianza" : "⚠ La clave no tiene restricción de IP", r.restrictions.ip_restrict ? "good" : "warnc");
  }
  if (r.suggest_url) { line("Estas claves pertenecen a " + r.suggest_url.replace("https://", "") + ": lo he seleccionado como servidor de pruebas.", "warnc"); applyConfig({ ENTORNO_PRUEBAS_URL: r.suggest_url }); }
}
// «Probar conexión» guarda primero lo que hayas escrito en ese recuadro y luego prueba con las claves guardadas.
async function runTest(env, ids, box) {
  box.textContent = "Probando…";
  const s = await saveTyped(ids);
  if (!s.ok) { box.replaceChildren(h("div", { class: "bad", text: "✘ " + s.errors.join(" · ") })); return; }
  if (s.saved) toast("Claves guardadas. Probando la conexión…", "ok");
  showTest(box, await api("/api/test_connection", { env }));
  poll();
}
async function clearCreds(names, label) {
  if (!(await ask({ title: "Borrar claves de " + label, text: "Se eliminarán del Administrador de credenciales de Windows. Podrás volver a pegarlas cuando quieras.", okLabel: "Borrar" }))) return;
  const r = await api("/api/credentials", { clear: names });
  toast(r.ok ? "Claves de " + label + " borradas." : r.errors.join(" · "), r.ok ? "ok" : "err");
  await loadConfig(); poll();
}
$("btnClearTestnet").onclick = () => clearCreds(["testnet_api_key", "testnet_api_secret"], "TESTNET");
$("btnClearReal").onclick = () => clearCreds(["real_api_key", "real_api_secret"], "la cuenta REAL");
$("btnTestTestnet").onclick = () => runTest("testnet", ["testnet_api_key", "testnet_api_secret"], $("resTestnet"));
$("btnTestReal").onclick = () => runTest("real", ["real_api_key", "real_api_secret"], $("resReal"));
$("btnCopyIp").onclick = async () => { try { await navigator.clipboard.writeText($("ipBox").textContent); toast("IP copiada", "ok"); } catch (e) { toast("Selecciónala y cópiala manualmente", "err"); } };
$("btnRefreshIp").onclick = async () => { const r = await api("/api/public_ip"); $("ipBox").textContent = r.ip || "no disponible"; };

$("btnTgPair").onclick = async () => {
  const r = await api("/api/telegram/pair", {});
  if (r.ok) { $("tgPairCmd").textContent = "/start " + r.code; $("tgPair").hidden = false; toast("Envía el código a @" + r.bot, "ok"); poll(); } else toast(r.error, "err");
};
$("btnTgTest").onclick = async () => { const r = await api("/api/telegram/test", {}); toast(r.ok ? "Mensaje enviado" : r.error, r.ok ? "ok" : "err"); };
$("btnTgUnlink").onclick = async () => { if (await ask({ title: "Desvincular Telegram", text: "Dejarás de recibir avisos hasta volver a vincular.", okLabel: "Desvincular" })) { await api("/api/telegram/unlink", {}); poll(); } };
function renderTelegram(s) {
  const tg = s.telegram; chip($("tgState"), tg.linked ? "Vinculado ✔" : tg.token ? "Sin vincular" : "Sin token", tg.linked ? "ok" : tg.token ? "warn" : "");
  const show = !!tg.pairing_code && tg.pairing_left > 0 && !tg.linked; $("tgPair").hidden = !show;
  if (show) { $("tgPairCmd").textContent = "/start " + tg.pairing_code; const m = Math.floor(tg.pairing_left / 60), sec = String(tg.pairing_left % 60).padStart(2, "0"); $("tgLeft").textContent = m + ":" + sec; }
}

/* ------------------------------------------------------------------ backtest & tooltips */
$("btHasta").value = new Date().toISOString().slice(0, 10);
function fillSel(id, opts) { $(id).replaceChildren(...opts.map(([v, l]) => h("option", { value: v, text: l }))); }
const CFG = ["__cfg__", "(config)"];
fillSel("btTend", [CFG, ...TREND.map((t) => [t, t])]); fillSel("btSup", [CFG, ["", "(ninguno)"], ...TREND.map((t) => [t, t])]);
fillSel("btZona", [CFG, ...PULL.map((t) => [t, t])]); fillSel("btGatillo", [CFG, ...TRIG.map((t) => [t, t])]);

const PARAM_EXPLANATIONS = {
  btStrat: {
    title: "Estrategia a simular",
    desc: "Define qué reglas matemáticas e indicadores se evalúan para generar entradas (ej. Estrategia 1: Tendencia 12h/1d + retroceso RSI < 20 y toque de Banda Bollinger inferior en 15m/30m para compra alcista)."
  },
  btDesde: {
    title: "Fecha Desde",
    desc: "Día de inicio del histórico descargado de Binance. Para un análisis estadístico robusto se recomienda probar con al menos 1 o 2 años de datos reales."
  },
  btHasta: {
    title: "Fecha Hasta",
    desc: "Fecha final de la simulación histórica. Por defecto se completa con la fecha de hoy para evaluar hasta la última vela cerrada."
  },
  btSlip: {
    title: "Slippage (Deslizamiento)",
    desc: "Diferencia estimada entre el precio al cierre de la vela gatillo y el precio real de llenado de la orden en Binance (0.0002 equivale a un 0.02% de tolerancia)."
  },
  btTend: {
    title: "Tendencia (Fase 1)",
    desc: "Temporalidad macro principal (ej. 1d o 4h) para determinar la dirección del mercado según la SMA(200) y el cruce de las EMA(20) y EMA(50)."
  },
  btSup: {
    title: "Filtro superior",
    desc: "Filtro macro mayor opcional (ej. 1w semanal o 2d). Si está activo, bloquea compras si la tendencia semanal es bajista, y viceversa."
  },
  btZona: {
    title: "Retrocesos (Fase 2)",
    desc: "Temporalidad donde se evalúa si el precio ha retrocedido hacia la zona de valor (entre EMA20 y EMA50 o niveles Fibonacci 50%-61.8%)."
  },
  btGatillo: {
    title: "Gatillo (Fase 3)",
    desc: "Temporalidad rápida (ej. 15m o 5m) donde se confirma el patrón de vela japonesa (Pin Bar, Barra Envolvente, etc.) para entrar al mercado."
  },
  btMart: {
    title: "Martingala",
    desc: "Permite promediar posición con recompras límite en soportes o niveles Fibonacci en caso de retroceso adverso. 'No' opera con entrada única fija."
  },
  btMargen: {
    title: "Margen por operación (USDT)",
    desc: "Capital en dólares bloqueado como margen aislado para la posición. Si usas martingala, es el presupuesto total repartido entre los niveles."
  },
  btRiesgoM: {
    title: "Riesgo máx. martingala (USDT)",
    desc: "Pérdida monetaria máxima permitida en dólares si todas las recompras se ejecutan y el precio toca el Stop Loss final."
  },
  btVentana: {
    title: "Ventana de agotamiento",
    desc: "Cantidad máxima de velas (1 a 5) que el bot espera para que se confirme el gatillo tras entrar a la zona de valor antes de cancelar la señal."
  }
};

function initParamTips() {
  const box = $("btParamHelp"), text = $("btParamHelpText");
  if (!box || !text) return;
  const defHtml = "<b>Pasa el cursor por cualquier parámetro</b> para ver aquí su explicación detallada y su impacto en la estrategia.";
  let timeout = null;

  document.querySelectorAll(".has-tip").forEach((el) => {
    const key = el.dataset.param;
    const info = PARAM_EXPLANATIONS[key];
    if (!info) return;

    el.setAttribute("title", info.title + ": " + info.desc);
    const inp = el.querySelector("input, select");
    if (inp) inp.setAttribute("title", info.title + ": " + info.desc);

    const onEnter = () => {
      if (timeout) clearTimeout(timeout);
      text.innerHTML = `<b>${info.title}:</b> ${info.desc}`;
      box.classList.add("highlight");
    };
    const onLeave = () => {
      timeout = setTimeout(() => {
        text.innerHTML = defHtml;
        box.classList.remove("highlight");
      }, 3500);
    };

    el.addEventListener("mouseenter", onEnter);
    el.addEventListener("mouseleave", onLeave);
    if (inp) {
      inp.addEventListener("focus", onEnter);
      inp.addEventListener("blur", onLeave);
    }
  });
}

$("btnBtRun").onclick = async () => {
  const pMsg = $("btMsg");
  const pBar = $("btBar");
  const progBox = $("btProg");
  
  // Sanitizar inputs (permitir comas en números en teclado español)
  const slipVal = ($("btSlip").value || "0.0002").replace(",", ".").trim();
  const margenVal = ($("btMargen").value || "").replace(",", ".").trim();
  const riesgoMVal = ($("btRiesgoM").value || "").replace(",", ".").trim();
  const desdeVal = ($("btDesde").value || "2022-01-01").trim();
  const hastaVal = ($("btHasta").value || new Date().toISOString().slice(0, 10)).trim();
  const stratVal = $("btStrat") ? $("btStrat").value : "";

  // Feedback visual inmediato
  pMsg.hidden = false;
  pMsg.textContent = "⏳ Conectando con Binance y preparando simulación...";
  pMsg.className = "bt-msg running";
  progBox.hidden = false;
  pBar.style.width = "4%";
  $("btnBtRun").disabled = true;
  $("btnBtCancel").hidden = false;

  const r = await api("/api/backtest/start", {
    estrategia: stratVal,
    desde: desdeVal,
    hasta: hastaVal,
    tendencia: $("btTend").value,
    superior: $("btSup").value,
    zona: $("btZona").value,
    gatillo: $("btGatillo").value,
    ventana: $("btVentana").value,
    slippage: slipVal,
    martingala: $("btMart").value,
    margen: margenVal,
    riesgo_mart: riesgoMVal
  });

  if (!r.ok) {
    pMsg.textContent = "✘ " + (r.error || "No se pudo iniciar el backtest.");
    pMsg.className = "bt-msg error";
    $("btnBtRun").disabled = false;
    $("btnBtCancel").hidden = true;
    progBox.hidden = true;
    toast(r.error || "Error al iniciar backtest", "err");
  } else {
    $("btResult").hidden = true;
    shownBt = 0;
    toast("Simulación iniciada correctamente", "ok");
    poll();
  }
};

$("btnBtCancel").onclick = async () => {
  await api("/api/backtest/cancel", {});
  toast("Cancelación solicitada", "warn");
};

let shownBt = 0;
function renderBacktest(s) {
  const bt = s.backtest;
  const isRunning = !!bt.running;
  $("btProg").hidden = !isRunning && !(bt.progress && bt.progress < 1.0);
  $("btnBtCancel").hidden = !isRunning;
  $("btnBtRun").disabled = isRunning;
  $("btBar").style.width = Math.round((bt.progress || 0) * 100) + "%";
  
  const pMsg = $("btMsg");
  if (bt.error) {
    pMsg.textContent = "✘ " + bt.error;
    pMsg.className = "bt-msg error";
    pMsg.hidden = false;
  } else if (bt.message) {
    pMsg.textContent = (isRunning ? "⏳ " : "✔ ") + bt.message + (isRunning && bt.progress ? " (" + Math.round(bt.progress * 100) + "%)" : "");
    pMsg.className = isRunning ? "bt-msg running" : "bt-msg success";
    pMsg.hidden = false;
  } else if (!isRunning) {
    pMsg.hidden = true;
  }
  
  if (bt.has_result && !bt.running && !shownBt) {
    shownBt = 1;
    api("/api/backtest/result").then(showBtResult);
  }
}
const METRICS = [["operaciones", "Operaciones"], ["winrate_pct", "Aciertos %"], ["neto_total_usdt", "Neto USDT"], ["retorno_pct", "Retorno %"], ["capital_final", "Capital final"], ["expectativa_R", "Expectativa (R)"],
  ["profit_factor", "Profit factor"], ["max_drawdown_usdt", "Máx. drawdown USDT"], ["max_drawdown_pct", "Máx. drawdown %"], ["buy_hold_pct", "Comprar y mantener %"], ["largos", "Largos"], ["cortos", "Cortos"], ["con_martingala", "Con niveles ejecutados"], ["niveles_medios", "Niveles medios"]];
const FUNNEL = [["velas", "Velas evaluadas"], ["tendencia_larga", "Con tendencia alcista"], ["tendencia_corta", "Con tendencia bajista"], ["standby_lateral", "Mercado lateral (STANDBY)"], ["fase2_ok", "Fase 2 cumplida"], ["fase3_ok_senales", "Señales completas"], ["bloqueadas_riesgo", "Bloqueadas por riesgo"], ["operadas", "Operaciones"]];
function showBtResult(res) {
  if (!res) return; $("btResult").hidden = false;
  $("btNotes").replaceChildren(...res.notes.map((n) => h("p", { text: "• " + n })));
  $("btMetrics").replaceChildren(...METRICS.map(([k, l]) => { const v = res.metrics[k]; return h("div", { class: "metric" }, h("b", { text: v == null ? "–" : String(v) }), h("span", { text: l })); }));
  kv($("btFunnel"), FUNNEL.map(([k, l]) => [l, String(res.funnel[k])]));
  kv($("btYears"), Object.entries(res.by_year).map(([y, d]) => [y, d.operaciones + " op. · " + signed(d.neto) + " USDT", clsSign(d.neto)]));
  const tb = $("btTrades").querySelector("tbody"); tb.replaceChildren();
  for (const t of res.trades) tb.append(h("tr", {}, h("td", { text: new Date(t.entry_time).toLocaleString("es", { hour12: false }) }), h("td", { text: t.side }), h("td", { text: t.pattern }), h("td", { text: num(t.entry, 1) }), h("td", { text: num(t.exit, 1) }),
    h("td", { text: t.reason }), h("td", { class: clsSign(t.net), text: signed(t.net) }), h("td", { class: clsSign(t.r), text: signed(t.r) })));
  drawEquity($("btEquity"), res.equity);
}
function drawEquity(cv, pts) {
  const dpr = window.devicePixelRatio || 1, r = cv.getBoundingClientRect(); cv.width = r.width * dpr; cv.height = r.height * dpr;
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); const W = r.width, H = r.height; ctx.clearRect(0, 0, W, H);
  if (!pts || pts.length < 2) return;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]); const x0 = Math.min(...xs), x1 = Math.max(...xs); let y0 = Math.min(...ys), y1 = Math.max(...ys); const pad = (y1 - y0) * 0.1 || 1; y0 -= pad; y1 += pad;
  const X = (x) => 40 + (x - x0) / ((x1 - x0) || 1) * (W - 50), Y = (y) => 8 + (y1 - y) / (y1 - y0) * (H - 28);
  ctx.strokeStyle = css("--line"); ctx.fillStyle = css("--muted"); ctx.font = "11px Consolas, monospace";
  for (let i = 0; i <= 4; i++) { const v = y0 + (y1 - y0) * i / 4, y = Y(v); ctx.beginPath(); ctx.moveTo(40, y); ctx.lineTo(W - 4, y); ctx.stroke(); ctx.fillText(num(v, 0), 2, y + 4); }
  ctx.strokeStyle = css("--accent"); ctx.lineWidth = 1.8; ctx.beginPath();
  pts.forEach((p, i) => { const x = X(p[0]), y = Y(p[1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
  ctx.fillStyle = css("--muted"); ctx.fillText(new Date(x0).toISOString().slice(0, 10), 40, H - 4); ctx.textAlign = "right"; ctx.fillText(new Date(x1).toISOString().slice(0, 10), W - 4, H - 4); ctx.textAlign = "left";
}

/* ------------------------------------------------------------------ arranque */
loadConfig(); poll(); refreshChart(); initParamTips();
setInterval(poll, 2000); setInterval(refreshChart, 3000);


