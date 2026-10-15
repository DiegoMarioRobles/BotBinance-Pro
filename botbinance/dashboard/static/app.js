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
const CHART_TFS = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "16h", "20h", "1d", "2d", "3d", "1w", "1M"];

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
  ema20: false,
  ema50: false,
  sma200: false,
  bb: true,
  rsi: true,
  sr: false,
  fib: false,
  strat: true
};

const STRATEGY_INDICATORS = {
  estrategia_1: ["bb", "rsi", "strat"],
  estrategia_2: ["ema20", "ema50", "sma200", "fib", "strat"],
  estrategia_3: ["bb", "strat"],
  estrategia_4: ["ema20", "ema50", "strat"],
  estrategia_5: ["rsi", "sr", "strat"]
};

let currentActiveStrategy = null;

function applyStrategyIndicators(strategyId) {
  const allowed = STRATEGY_INDICATORS[strategyId] || ["bb", "rsi", "strat"];
  for (const k of Object.keys(indState)) {
    indState[k] = allowed.includes(k);
  }
  document.querySelectorAll(".ind-pill").forEach((btn) => {
    const ind = btn.dataset.ind;
    if (ind && indState.hasOwnProperty(ind)) {
      btn.classList.toggle("on", indState[ind]);
    }
  });
  if (window.chart) chart.draw();
}

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

const btnSyncStrat = $("btnSyncStratInds");
if (btnSyncStrat) {
  btnSyncStrat.onclick = () => {
    applyStrategyIndicators(currentActiveStrategy || (S && S.strategy) || "estrategia_1");
    toast("Indicadores configurados para la estrategia activa", "ok");
  };
}

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
    const keys = ["open_time", "open", "high", "low", "close", "volume", "ema20", "ema50", "sma200", "bb_mid", "bb_up", "bb_low", "rsi14"];
    const a = {}; keys.forEach((k) => (a[k] = (s[k] || []).slice()));
    const f = d.forming;
    if (f) {
      a.open_time.push(f.open_time); a.open.push(f.open); a.high.push(f.high); a.low.push(f.low); a.close.push(f.close); a.volume.push(f.volume);
      ["ema20", "ema50", "sma200", "bb_mid", "bb_up", "bb_low", "rsi14"].forEach((k) => a[k].push(null));
    }
    this.a = a; this.n = a.open_time.length;
    if (resetView) { this.count = Math.min(120, this.n); this.offset = 0; }
    this.draw();
  }
  geo() {
    const r = this.cv.parentElement.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1;
    const W = Math.max(300, r.width), H = Math.max(240, r.height);
    if (this.cv.width !== Math.round(W * dpr) || this.cv.height !== Math.round(H * dpr)) { this.cv.width = Math.round(W * dpr); this.cv.height = Math.round(H * dpr); }
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { W, H, L: 8, R: 68, T: 24, B: 28 };
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
    const pw = g.W - g.L - g.R, ph = g.H - g.T - g.B;
    const hasRsi = !!(indState.rsi && a.rsi14 && a.rsi14.length);
    const rsiH = hasRsi ? Math.min(105, Math.max(68, Math.round(ph * 0.23))) : 0;
    const volH = Math.round((ph - rsiH) * 0.15);
    const priceH = ph - rsiH - volH - (hasRsi ? 16 : 6);
    const bw = pw / this.count; const xOf = (i) => g.L + (i - i0 + 0.5) * bw;
    let lo = Infinity, hi = -Infinity, vmax = 0;
    for (let i = i0; i < i1; i++) { lo = Math.min(lo, a.low[i]); hi = Math.max(hi, a.high[i]); vmax = Math.max(vmax, a.volume[i]); }
    const ann = (this.d && this.d.annotations) || {}; const tr = ann.trade;
    if (tr) for (const k of ["sl", "tp", "entry"]) if (tr[k]) { lo = Math.min(lo, tr[k]); hi = Math.max(hi, tr[k]); }
    const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
    const yOf = (p) => g.T + (hi - p) / (hi - lo) * priceH;
    const text = css("--muted"), line = css("--line");

    // Distintivo / Watermark de la temporalidad en la esquina superior izquierda
    ctx.save();
    ctx.font = "bold 13px system-ui, -apple-system, sans-serif";
    ctx.fillStyle = css("--text");
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    const tfUpper = (this.tf || "1h").toUpperCase();
    const role = (this.d && this.d.role_desc) || (S && S.tf_roles && (
      this.tf === S.tf_roles.gatillo ? "Gatillo de entrada" :
      this.tf === S.tf_roles.zona ? "Zona de retroceso" :
      this.tf === S.tf_roles.tendencia ? "Tendencia macro" :
      this.tf === S.tf_roles.superior ? "Tendencia superior" : ""
    )) || "";
    ctx.fillText("BTC/USDT  ·  " + tfUpper + (role ? "  (" + role + ")" : ""), g.L + 8, 5);
    ctx.restore();

    ctx.font = "11px Consolas, monospace"; ctx.textBaseline = "middle";
    // rejilla de precio
    const step = niceStep((hi - lo) / 6);
    for (let p = Math.ceil(lo / step) * step; p < hi; p += step) {
      const y = yOf(p); ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(g.L, y); ctx.lineTo(g.W - g.R, y); ctx.stroke();
      ctx.fillStyle = text; ctx.textAlign = "left"; ctx.fillText(num(p, step < 1 ? 2 : 0), g.W - g.R + 6, y);
    }
    // separador de eje de tiempo
    ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(g.L, g.H - g.B); ctx.lineTo(g.W - g.R, g.H - g.B); ctx.stroke();
    // eje de tiempo
    ctx.textAlign = "center"; ctx.textBaseline = "top"; const every = Math.max(1, Math.round(95 / bw));
    for (let i = i0; i < i1; i += every) {
      const dt = new Date(a.open_time[i]);
      const tfCur = (this.tf || "").toLowerCase();
      let lab;
      if (["1w", "1m"].includes(tfCur)) {
        lab = dt.toLocaleDateString("es", { day: "2-digit", month: "short", year: "2-digit" });
      } else if (["1d", "2d", "3d"].includes(tfCur)) {
        lab = dt.toLocaleDateString("es", { day: "2-digit", month: "short" });
      } else if (["2h", "4h", "6h", "8h", "12h", "16h", "20h"].includes(tfCur)) {
        lab = dt.toLocaleDateString("es", { day: "2-digit", month: "numeric" }) + " " + dt.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit", hour12: false });
      } else {
        lab = dt.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit", hour12: false });
      }
      ctx.fillStyle = text; ctx.fillText(lab, xOf(i), g.H - g.B + 7);
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
    // bandas e indicadores de precio
    const series = (arr, color, w, dash) => {
      ctx.strokeStyle = color; ctx.lineWidth = w; ctx.setLineDash(dash || []); ctx.beginPath(); let on = false;
      for (let i = i0; i < i1; i++) { const v = arr[i]; if (v == null || isNaN(v)) { on = false; continue; } const x = xOf(i), y = yOf(v); on ? ctx.lineTo(x, y) : ctx.moveTo(x, y); on = true; }
      ctx.stroke(); ctx.setLineDash([]);
    };
    if (indState.bb && a.bb_up && a.bb_low) {
      // Sombreado de canal de Bollinger
      ctx.save();
      ctx.fillStyle = "rgba(41, 182, 246, 0.08)";
      ctx.beginPath();
      let bbInit = false;
      for (let i = i0; i < i1; i++) {
        const v = a.bb_up[i];
        if (v == null || isNaN(v)) continue;
        const x = xOf(i), y = yOf(v);
        if (!bbInit) { ctx.moveTo(x, y); bbInit = true; } else { ctx.lineTo(x, y); }
      }
      for (let i = i1 - 1; i >= i0; i--) {
        const v = a.bb_low[i];
        if (v == null || isNaN(v)) continue;
        ctx.lineTo(xOf(i), yOf(v));
      }
      if (bbInit) { ctx.closePath(); ctx.fill(); }
      ctx.restore();

      series(a.bb_up, "#29b6f6", 1.4);
      if (a.bb_mid) series(a.bb_mid, "rgba(41, 182, 246, 0.6)", 1.0, [4, 4]);
      series(a.bb_low, "#29b6f6", 1.4);
    }
    if (indState.sma200 && a.sma200) series(a.sma200, css("--sma200"), 1.4);
    if (indState.ema50 && a.ema50) series(a.ema50, css("--ema50"), 1.2);
    if (indState.ema20 && a.ema20) series(a.ema20, css("--ema20"), 1.2);

    // velas + volumen
    const up = css("--up"), dn = css("--down"), vu = css("--vol-up"), vd = css("--vol-down");
    const cw = Math.max(1, bw * 0.7);
    const volBaseY = g.T + priceH + 4 + volH;
    for (let i = i0; i < i1; i++) {
      const o = a.open[i], c = a.close[i], x = xOf(i), isUp = c >= o;
      ctx.strokeStyle = ctx.fillStyle = isUp ? up : dn; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, yOf(a.high[i])); ctx.lineTo(x, yOf(a.low[i])); ctx.stroke();
      const yo = yOf(o), yc = yOf(c); ctx.fillRect(x - cw / 2, Math.min(yo, yc), cw, Math.max(1, Math.abs(yo - yc)));
      ctx.fillStyle = isUp ? vu : vd; const vh = vmax ? a.volume[i] / vmax * volH : 0; ctx.fillRect(x - cw / 2, volBaseY - vh, cw, vh);
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

    // Subpanel de RSI (14)
    if (hasRsi) {
      const rsiTopY = g.T + priceH + volH + 12;
      const yRsi = (v) => rsiTopY + (100 - Math.max(0, Math.min(100, v))) / 100 * rsiH;

      // Línea divisoria del panel de RSI
      ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(g.L, rsiTopY - 4); ctx.lineTo(g.W - g.R, rsiTopY - 4); ctx.stroke();

      // Zona sombreada entre nivel 20 y 80
      // Subpanel de RSI con umbrales configurables y búsqueda del último valor válido
      const indCfg = (ann && ann.indicators_config) || (S && S.indicators) || {};
      const rsiOversold = Number(indCfg.rsi_oversold ?? (cfgValues && cfgValues.RSI_SOBREVENTA) ?? 35);
      const rsiOverbought = Number(indCfg.rsi_overbought ?? (cfgValues && cfgValues.RSI_SOBRECOMPRA) ?? 65);
      const rsiPeriod = Number(indCfg.rsi_period ?? (cfgValues && cfgValues.RSI_PERIODO) ?? 14);

      // Zona sombreada entre nivel sobreventa y sobrecompra
      ctx.fillStyle = "rgba(179, 136, 255, 0.05)";
      ctx.fillRect(g.L, yRsi(rsiOverbought), pw, yRsi(rsiOversold) - yRsi(rsiOverbought));

      // Nivel Sobrecompra
      ctx.strokeStyle = "rgba(239, 83, 80, 0.65)"; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(g.L, yRsi(rsiOverbought)); ctx.lineTo(g.W - g.R, yRsi(rsiOverbought)); ctx.stroke();
      ctx.fillStyle = css("--down"); ctx.font = "9.5px Consolas, monospace"; ctx.textAlign = "left";
      ctx.fillText(rsiOverbought + " Sobrecompra", g.W - g.R + 4, yRsi(rsiOverbought));

      // Nivel 50 (Centro)
      ctx.strokeStyle = "rgba(139, 152, 169, 0.3)"; ctx.setLineDash([2, 4]);
      ctx.beginPath(); ctx.moveTo(g.L, yRsi(50)); ctx.lineTo(g.W - g.R, yRsi(50)); ctx.stroke();
      ctx.fillStyle = text; ctx.fillText("50", g.W - g.R + 4, yRsi(50));

      // Nivel Sobreventa (Gatillo)
      ctx.strokeStyle = "rgba(38, 166, 154, 0.85)"; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(g.L, yRsi(rsiOversold)); ctx.lineTo(g.W - g.R, yRsi(rsiOversold)); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = css("--up"); ctx.font = "bold 9.5px Consolas, monospace";
      ctx.fillText(rsiOversold + " Sobreventa (Gatillo)", g.W - g.R + 4, yRsi(rsiOversold));

      // Curva del oscilador RSI
      ctx.strokeStyle = "#b388ff"; ctx.lineWidth = 1.8; ctx.beginPath();
      let rsiStarted = false;
      for (let i = i0; i < i1; i++) {
        const val = a.rsi14[i];
        if (val == null || isNaN(val)) { rsiStarted = false; continue; }
        const x = xOf(i), y = yRsi(val);
        rsiStarted ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        rsiStarted = true;
      }
      ctx.stroke();

      // Puntos destacados cuando RSI <= rsiOversold (Gatillo)
      ctx.fillStyle = css("--up");
      for (let i = i0; i < i1; i++) {
        const val = a.rsi14[i];
        if (val != null && val <= rsiOversold) {
          const x = xOf(i), y = yRsi(val);
          ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2); ctx.fill();
        }
      }

      // Encabezado del oscilador con valor actual (búsqueda hacia atrás del último valor numérico válido)
      let lastRsi = null;
      for (let k = this.n - 1; k >= 0; k--) {
        if (a.rsi14[k] != null && !isNaN(a.rsi14[k])) {
          lastRsi = a.rsi14[k];
          break;
        }
      }
      ctx.font = "bold 11px system-ui, sans-serif";
      ctx.textAlign = "left"; ctx.textBaseline = "bottom";
      let rsiTxt = "RSI (" + rsiPeriod + "): " + (lastRsi != null ? lastRsi.toFixed(1) : "–");
      if (lastRsi != null && lastRsi <= rsiOversold) {
        ctx.fillStyle = css("--up");
        rsiTxt += "  ⚡ SOBREVENTA ≤ " + rsiOversold + " (Condición Gatillo cumplida)";
      } else if (lastRsi != null && lastRsi >= rsiOverbought) {
        ctx.fillStyle = css("--down");
        rsiTxt += "  ⚠ SOBRECOMPRA ≥ " + rsiOverbought;
      } else {
        ctx.fillStyle = "#b388ff";
      }
      ctx.fillText(rsiTxt, g.L + 8, rsiTopY - 2);
    }

    // cursor y tooltip
    const m = this.mouse;
    if (m && m.x > g.L && m.x < g.W - g.R && m.y > g.T && m.y < g.H - g.B) {
      ctx.strokeStyle = text; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(m.x, g.T); ctx.lineTo(m.x, g.H - g.B); ctx.moveTo(g.L, m.y); ctx.lineTo(g.W - g.R, m.y); ctx.stroke(); ctx.setLineDash([]);
      const i = Math.min(i1 - 1, Math.max(i0, Math.floor(i0 + (m.x - g.L) / bw)));
      const f = (v, nd) => v == null ? "–" : num(v, nd === undefined ? 1 : nd);
      const dt = new Date(a.open_time[i]);
      let lines = [
        `${dt.toLocaleString("es", { hour12: false })}  (UTC ${dt.toISOString().slice(5, 16).replace("T", " ")})`,
        `O ${f(a.open[i])}  H ${f(a.high[i])}`,
        `L ${f(a.low[i])}  C ${f(a.close[i])}`,
        `Vol ${f(a.volume[i], 1)}`
      ];
      const indCfg = (ann && ann.indicators_config) || (S && S.indicators) || {};
      const rsiOversold = Number(indCfg.rsi_oversold ?? (cfgValues && cfgValues.RSI_SOBREVENTA) ?? 35);
      const rsiOverbought = Number(indCfg.rsi_overbought ?? (cfgValues && cfgValues.RSI_SOBRECOMPRA) ?? 65);
      const rsiPeriod = Number(indCfg.rsi_period ?? (cfgValues && cfgValues.RSI_PERIODO) ?? 14);
      if (indState.rsi && a.rsi14 && a.rsi14[i] != null) {
        const isOv = a.rsi14[i] <= rsiOversold;
        const isOb = a.rsi14[i] >= rsiOverbought;
        const tag = isOv ? ` (Sobreventa ≤ ${rsiOversold} ✔)` : isOb ? ` (Sobrecompra ≥ ${rsiOverbought})` : "";
        lines.push(`RSI(${rsiPeriod}): ${f(a.rsi14[i], 1)}${tag}`);
      }
      if (indState.bb && a.bb_up && a.bb_up[i] != null) {
        const bbPer = Number(indCfg.bb_period ?? (cfgValues && cfgValues.BB_PERIODO) ?? 20);
        lines.push(`BB(${bbPer}) Inf: ${f(a.bb_low[i])}  Med: ${f(a.bb_mid ? a.bb_mid[i] : null)}  Sup: ${f(a.bb_up[i])}`);
      }
      if (indState.ema20 && a.ema20 && a.ema20[i] != null) {
        const p20 = Number(indCfg.ema_fast ?? (cfgValues && cfgValues.EMA_RAPIDA) ?? 20);
        const p50 = Number(indCfg.ema_slow ?? (cfgValues && cfgValues.EMA_LENTA) ?? 50);
        lines.push(`EMA${p20}: ${f(a.ema20[i])}${indState.ema50 && a.ema50 ? `  EMA${p50}: ` + f(a.ema50[i]) : ""}`);
      }
      if (indState.sma200 && a.sma200 && a.sma200[i] != null) {
        const p200 = Number(indCfg.sma_trend ?? (cfgValues && cfgValues.SMA_TENDENCIA) ?? 200);
        lines.push(`SMA${p200}: ${f(a.sma200[i])}`);
      }
      this.tip.textContent = lines.join("\n");
      this.tip.hidden = false; this.tip.style.left = Math.min(m.x + 14, g.W - 250) + "px"; this.tip.style.top = Math.max(4, m.y - 90) + "px";
    } else this.tip.hidden = true;
  }
}
function niceStep(raw) { const p = Math.pow(10, Math.floor(Math.log10(raw))); const f = raw / p; return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p; }
const chart = new Chart($("chart"), $("tip"));
$("zoomIn").onclick = () => chart.zoomBy(1 / 1.3); $("zoomOut").onclick = () => chart.zoomBy(1.3);
let chartTf = null;
try { chartTf = localStorage.getItem("bot_chart_tf") || null; } catch (e) {}
let lastCandleTf = null;
let currentChartReqId = 0;

function setChartLoading(loading, msg) {
  const overlay = $("chartLoadingOverlay");
  if (!overlay) return;
  if (loading) {
    if (msg) overlay.querySelector("span").textContent = msg;
    overlay.classList.add("active");
  } else {
    overlay.classList.remove("active");
  }
}

function syncTfUI(tf) {
  const sel = $("tfSelect");
  if (sel && sel.value !== tf) sel.value = tf;
  const pills = document.querySelectorAll("#tfQuickPills button");
  pills.forEach((b) => b.classList.toggle("on", b.dataset.tf === tf));
}

function setChartTimeframe(newTf) {
  if (!newTf) return;
  chartTf = newTf;
  try { localStorage.setItem("bot_chart_tf", newTf); } catch (e) {}
  syncTfUI(newTf);
  refreshChart(true);
}

function initTfControls() {
  const sel = $("tfSelect");
  if (sel && !sel.options.length) {
    CHART_TFS.forEach((t) => sel.append(h("option", { value: t, text: t })));
    sel.addEventListener("change", () => setChartTimeframe(sel.value));
  }
  const pills = document.querySelectorAll("#tfQuickPills button");
  pills.forEach((b) => {
    b.addEventListener("click", () => setChartTimeframe(b.dataset.tf));
  });
  const current = chartTf || window.__triggerTf || "1h";
  syncTfUI(current);
}

async function refreshChart(showLoading = false) {
  const reqId = ++currentChartReqId;
  const tf = chartTf || (window.__triggerTf || "1h");
  if (showLoading) setChartLoading(true, "⌛ Cargando velas " + tf + "…");
  try {
    const d = await api("/api/candles?tf=" + tf + "&n=400");
    if (!d || reqId !== currentChartReqId) return;
    const resolvedTf = d.tf || tf;
    chart.tf = resolvedTf;
    syncTfUI(resolvedTf);
    const reset = lastCandleTf !== resolvedTf;
    lastCandleTf = resolvedTf;
    chart.setData(d, reset);
  } catch (e) {
    if (showLoading) toast("No se pudieron cargar las velas de " + tf + ": reintentando…", "err");
  } finally {
    if (reqId === currentChartReqId) setChartLoading(false);
  }
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
  if (!chartTf) syncTfUI(s.trigger_tf || "1h");
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

  // resumen financiero y estado de operaciones en encabezado
  const openCount = (s.trade ? 1 : 0) + (s.position ? 1 : 0);
  const topOpBadge = $("mBadgeOpCount");
  const topOpCount = $("topOpCount");
  if (topOpCount) topOpCount.textContent = String(openCount);
  if (topOpBadge) {
    topOpBadge.classList.toggle("active", openCount > 0);
    topOpBadge.classList.toggle("idle", openCount === 0);
    topOpBadge.title = openCount > 0 ? "¡Hay " + openCount + " operación abierta en curso!" : "Sin operaciones abiertas actualmente";
  }
  const bannerOpCount = $("bannerOpCountNum");
  if (bannerOpCount) bannerOpCount.textContent = String(openCount);

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

  // indicadores activos
  if (s.indicators) {
    updateIndicatorPills({
      EMA_RAPIDA: s.indicators.ema_fast,
      EMA_LENTA: s.indicators.ema_slow,
      SMA_TENDENCIA: s.indicators.sma_trend,
      BB_PERIODO: s.indicators.bb_period,
      BB_DESVIACION: s.indicators.bb_dev,
      RSI_PERIODO: s.indicators.rsi_period,
    });
  }

  // estrategia y puntos de ejecución
  if (s.strategy && s.strategy !== currentActiveStrategy) {
    currentActiveStrategy = s.strategy;
    applyStrategyIndicators(s.strategy);
  }
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
    if (epTR) epTR.textContent = ep.target_rsi != null ? (ep.side === "LONG" ? "≤ " : "≥ ") + ep.target_rsi : "–";
    if (epMsg) {
      epMsg.textContent = ep.message || "Evaluando condiciones…";
      epMsg.className = "exec-msg" + (ep.target_reached ? " reached" : "");
    }
  }

  // Letrero llamativo de estado de la operación (Rojo = Sin Operación / Verde = Operación en Curso)
  const banner = $("opStatusBanner");
  const bBadgeText = $("opBannerBadgeText");
  const bHeadline = $("opBannerHeadline");
  const bMetrics = $("opBannerMetrics");
  const bAction = $("opBannerAction");
  const tr = s.trade;

  if (banner && bBadgeText && bHeadline && bMetrics) {
    bMetrics.replaceChildren();
    const makePill = (lbl, val, cls) => {
      const p = h("div", { class: "op-metric-pill" });
      p.append(h("span", { class: "op-metric-lbl", text: lbl }));
      p.append(h("b", { class: "op-metric-val " + (cls || ""), text: val }));
      return p;
    };
    if (tr) {
      banner.className = "op-status-banner active";
      bBadgeText.textContent = "OPERACIÓN EN CURSO · " + tr.side + " " + (s.symbol || "BTC/USDT") + " (" + (tr.leverage || fin.leverage || 5) + "x)";
      bHeadline.textContent = "Patrón: " + (tr.pattern || "Estrategia") + (tr.be_done ? " · Break-even activo (SL en entrada)" : "");
      if (bannerOpCount) bannerOpCount.textContent = String(openCount);
      if (bAction) bAction.hidden = false;
      bMetrics.append(makePill("Entrada", usd(tr.entry, 1)));
      bMetrics.append(makePill("Precio Marca", usd(tr.mark || s.price, 1)));
      bMetrics.append(makePill("PnL No Realizado", signed(tr.upnl, 2) + " USDT (" + signed(tr.r_now, 2) + " R)", clsSign(tr.upnl)));
      bMetrics.append(makePill("Stop Loss", usd(tr.sl, 1) + (tr.loss_sl_usdt ? " [-$" + num(tr.loss_sl_usdt, 2) + "]" : "")));
      bMetrics.append(makePill("Take Profit", usd(tr.tp, 1) + (tr.gain_tp_usdt ? " [+$" + num(tr.gain_tp_usdt, 2) + "]" : "")));
      bMetrics.append(makePill("Margen Propio", usd(tr.margin || fin.margin_config || 20, 1)));
    } else if (s.position) {
      banner.className = "op-status-banner active";
      bBadgeText.textContent = "POSICIÓN EXTERNA ACTIVA · " + num(s.position.amt, 4) + " BTC";
      bHeadline.textContent = "Posición detectada en la cuenta de Binance ajena al bot";
      if (bannerOpCount) bannerOpCount.textContent = String(openCount);
      if (bAction) bAction.hidden = false;
      bMetrics.append(makePill("Entrada", usd(s.position.entry, 1)));
      bMetrics.append(makePill("Precio Marca", usd(s.price, 1)));
      bMetrics.append(makePill("PnL No Realizado", signed(s.position.upnl, 2) + " USDT", clsSign(s.position.upnl)));
    } else {
      banner.className = "op-status-banner idle";
      bBadgeText.textContent = "SIN OPERACIÓN ACTIVA";
      if (bannerOpCount) bannerOpCount.textContent = "0";
      const stratName = s.strategy_info ? (s.strategy_info.short_name || s.strategy_info.name) : "Estrategia";
      bHeadline.textContent = "Monitoreando mercado en tiempo real · Buscando señal de entrada según " + stratName;
      if (bAction) bAction.hidden = true;
      bMetrics.append(makePill("Precio BTC", s.price ? usd(s.price, 1) : "–"));
      if (ep && ep.target_price) {
        bMetrics.append(makePill("Gatillo Precio (BB Inf)", usd(ep.target_price, 1)));
      }
      if (ep && ep.current_rsi != null) {
        const rsiOv = (s.config && Number(s.config.RSI_SOBREVENTA)) || 35;
        const rsiTarget = ep.target_rsi != null ? "≤ " + ep.target_rsi : "≤ " + rsiOv;
        bMetrics.append(makePill("RSI Actual", num(ep.current_rsi, 1) + " (Gatillo " + rsiTarget + ")", ep.current_rsi <= rsiOv ? "pos" : ""));
      }
      const phState = ev.phase3_ok ? "Fase 3: Gatillo cumplido ✔" : (ev.phase2_ok ? "Fase 2 cumplida · Esperando gatillo" : (ev.phase1_ok ? "Fase 1: Tendencia confirmada" : "Fase 1: Evaluando tendencia"));
      bMetrics.append(makePill("Estado Fases", phState));
    }
  }

  // posición
  const pb = $("posBox");
  const hasTrade = Boolean(tr || s.position);
  $("btnClose").hidden = !hasTrade;
  if ($("btnHeaderForceClose")) $("btnHeaderForceClose").style.display = hasTrade ? "inline-flex" : "none";
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
const handleForceClose = async () => {
  if (await ask({
    title: "🚨 Cierre Forzado a Mercado",
    text: "Se cerrará de INMEDIATO la posición activa de BTC a precio de mercado y se cancelarán todas las órdenes de Stop Loss y Take Profit pendientes en Binance.\n\n¿Confirmas el cierre forzado ahora?",
    okLabel: "🚨 Sí, Cerrar Posición Ahora",
  })) {
    const r = await api("/api/close_position", {});
    toast(r ? r.message : "Orden de cierre enviada", "ok");
    poll();
  }
};
$("btnClose").onclick = handleForceClose;
if ($("btnBannerClose")) $("btnBannerClose").onclick = handleForceClose;
if ($("btnHeaderForceClose")) $("btnHeaderForceClose").onclick = handleForceClose;

/* ------------------------------------------------------------------ configuración */
const TREND = ["4h", "8h", "12h", "16h", "20h", "1d", "2d", "3d", "1w", "1M"];
const PULL = ["5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"];
const TRIG = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"];

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

const IND_PARAMS = [
  ["RSI_PERIODO", "RSI · Período", 1, "Número de velas para el cálculo del RSI (estándar: 14)."],
  ["RSI_SOBREVENTA", "RSI · Sobreventa (Gatillo Long)", 1, "Nivel para buscar compras (ej: 20, 25, 30 o 35)."],
  ["RSI_SOBRECOMPRA", "RSI · Sobrecompra (Gatillo Short)", 1, "Nivel para buscar ventas (ej: 65, 70 u 80)."],
  ["BB_PERIODO", "Bollinger · Período", 1, "Media central de Bandas de Bollinger (estándar: 20)."],
  ["BB_DESVIACION", "Bollinger · Desviaciones", 0.1, "Multiplicador de desviación estándar (estándar: 2.0)."],
  ["EMA_RAPIDA", "Media Móvil Rápida (EMA)", 1, "Período de la EMA rápida (estándar: 20)."],
  ["EMA_LENTA", "Media Móvil Lenta (EMA)", 1, "Período de la EMA lenta (estándar: 50)."],
  ["SMA_TENDENCIA", "Media Tendencia Macro (SMA)", 1, "Período de la SMA de tendencia macro (estándar: 200)."],
  ["ATR_PERIODO", "ATR · Período Volatilidad", 1, "Período para el Average True Range (estándar: 14)."],
  ["VENTANA_AGOTAMIENTO", "Ventana de rebote (velas)", 1, "Velas recientes evaluadas para el toque de banda y RSI (estándar: 3)."],
  ["STOCH_SOBREVENTA", "Estocástico · Sobreventa", 1, "Oscilador estocástico sobreventa (estándar: 20)."],
  ["STOCH_SOBRECOMPRA", "Estocástico · Sobrecompra", 1, "Oscilador estocástico sobrecompra (estándar: 80)."],
];

function updateIndicatorPills(cfg) {
  if (!cfg) return;
  const ema20 = cfg.EMA_RAPIDA || 20;
  const ema50 = cfg.EMA_LENTA || 50;
  const sma200 = cfg.SMA_TENDENCIA || 200;
  const bbPer = cfg.BB_PERIODO || 20;
  const bbDev = cfg.BB_DESVIACION || 2.0;
  const rsiPer = cfg.RSI_PERIODO || 14;
  if ($("lblEma20")) $("lblEma20").textContent = `EMA ${ema20}`;
  if ($("lblEma50")) $("lblEma50").textContent = `EMA ${ema50}`;
  if ($("lblSma200")) $("lblSma200").textContent = `SMA ${sma200}`;
  if ($("lblBb")) $("lblBb").textContent = `Bollinger (${bbPer}, ${bbDev})`;
  if ($("lblRsi")) $("lblRsi").textContent = `RSI (${rsiPer})`;
}

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
  for (const [key, , kind] of [...RISK, ...TFS, ...MART, ...ADV, ...IND_PARAMS]) {
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
  buildForm($("formRisk"), RISK); buildForm($("formTfs"), TFS); buildForm($("formMart"), MART); buildForm($("formAdv"), ADV);
  if ($("formIndParams")) buildForm($("formIndParams"), IND_PARAMS);
  fillForm(c.values);
  updateIndicatorPills(c.values);
  cfgLoaded = true;
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
        currentActiveStrategy = topSel.value;
        applyStrategyIndicators(topSel.value);
        await applyConfig({ ESTRATEGIA: topSel.value });
      };
    }
    if (topSel && c.active_strategy) {
      topSel.value = c.active_strategy;
      currentActiveStrategy = c.active_strategy;
      applyStrategyIndicators(c.active_strategy);
    }
    if (btSel && !btSel.options.length) {
      for (const [id, info] of Object.entries(c.strategies)) {
        const descText = info.desc || info.description || "";
        btSel.append(h("option", { value: id, text: info.name + (descText ? " — " + descText : "") }));
      }
      if (c.active_strategy) btSel.value = c.active_strategy;
    }
  }

  if (c.values) {
    if (c.values.APALANCAMIENTO && $("btLev") && !$("btLev").value) $("btLev").value = c.values.APALANCAMIENTO;
    if (c.values.MARGEN_EN_USO_POR_OPERACION && $("btMargen") && !$("btMargen").value) $("btMargen").placeholder = c.values.MARGEN_EN_USO_POR_OPERACION;
    if (typeof updateBtNotional === "function") updateBtNotional();
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
  for (const [key, , kind] of [...RISK, ...TFS, ...MART, ...ADV, ...IND_PARAMS]) { const el = $("f_" + key); if (el) values[key] = kind === "bool" ? el.checked : el.value; }
  values.TELEGRAM_ACTIVO = $("TELEGRAM_ACTIVO").checked; values.TELEGRAM_AVISAR_SENALES_BLOQUEADAS = $("TELEGRAM_AVISAR_SENALES_BLOQUEADAS").checked;
  const r = await applyConfig(values);
  $("cfgMsg").textContent = r && r.ok ? "Guardado ✔" : "";
  if (r && r.ok) { updateIndicatorPills(cfgValues); refreshChart(true); }
};

if ($("btnSaveIndParams")) $("btnSaveIndParams").onclick = () => $("btnSaveCfg").onclick();
if ($("btnResetIndDefaults")) $("btnResetIndDefaults").onclick = async () => {
  const defaults = {
    RSI_PERIODO: 14, RSI_SOBREVENTA: 35.0, RSI_SOBRECOMPRA: 65.0,
    BB_PERIODO: 20, BB_DESVIACION: 2.0, EMA_RAPIDA: 20, EMA_LENTA: 50,
    SMA_TENDENCIA: 200, ATR_PERIODO: 14, VENTANA_AGOTAMIENTO: 3,
    STOCH_SOBREVENTA: 20.0, STOCH_SOBRECOMPRA: 80.0
  };
  const r = await applyConfig(defaults);
  if (r && r.ok) { updateIndicatorPills(cfgValues); refreshChart(true); }
};

if ($("btnResetStrat1Defaults")) {
  $("btnResetStrat1Defaults").onclick = async () => {
    const def = {
      ESTRATEGIA: "estrategia_1",
      TF_TENDENCIA: "1d",
      TF_TENDENCIA_SUPERIOR: "1w",
      TF_ZONA: "4h",
      TF_FIBONACCI: "1d",
      TF_GATILLO: "1h",
      ADX_TENDENCIA_MIN: 25.0,
      ADX_LATERAL_MAX: 22.0,
      RSI_SOBREVENTA: 35.0,
      RSI_SOBRECOMPRA: 65.0,
      VENTANA_AGOTAMIENTO: 1,
      RATIO_RIESGO_BENEFICIO: 3.0,
      BB_PERIODO: 20,
      BB_DESVIACION: 2.0,
      RSI_PERIODO: 14,
    };
    fillForm(def);
    const r = await applyConfig(def);
    if (r && r.ok) {
      toast("✔ Parámetros predeterminados oficiales de Estrategia 1 restaurados", "ok");
      if (typeof updateIndicatorPills === "function") updateIndicatorPills(cfgValues);
      if (typeof refreshChart === "function") refreshChart(true);
    }
  };
}

if ($("btnLoadStrat1Recommended")) {
  $("btnLoadStrat1Recommended").onclick = async () => {
    const rec = {
      ESTRATEGIA: "estrategia_1",
      TF_TENDENCIA: "4h",
      TF_TENDENCIA_SUPERIOR: "",
      TF_ZONA: "15m",
      TF_FIBONACCI: "4h",
      TF_GATILLO: "15m",
      ADX_TENDENCIA_MIN: 20.0,
      ADX_LATERAL_MAX: 18.0,
      RSI_SOBREVENTA: 28.0,
      RSI_SOBRECOMPRA: 72.0,
      VENTANA_AGOTAMIENTO: 3,
      RATIO_RIESGO_BENEFICIO: 1.5,
      BB_PERIODO: 20,
      BB_DESVIACION: 2.0,
      RSI_PERIODO: 14,
    };
    fillForm(rec);
    const r = await applyConfig(rec);
    if (r && r.ok) {
      toast("✔ Parámetros recomendados para Estrategia 1 (Diaria) aplicados con éxito", "ok");
      if (typeof updateIndicatorPills === "function") updateIndicatorPills(cfgValues);
      if (typeof refreshChart === "function") refreshChart(true);
    }
  };
}

/* Modal Interactivo de Parámetros de Indicadores */
function openIndModal() {
  const dlg = $("dlgIndParams");
  if (!dlg) return;
  const c = cfgValues || {};
  const setVal = (id, key, def) => {
    const el = $("m_" + id);
    if (el) el.value = c[key] != null ? c[key] : def;
  };
  setVal("RSI_PERIODO", "RSI_PERIODO", 14);
  setVal("RSI_SOBREVENTA", "RSI_SOBREVENTA", 35.0);
  setVal("RSI_SOBRECOMPRA", "RSI_SOBRECOMPRA", 65.0);
  setVal("BB_PERIODO", "BB_PERIODO", 20);
  setVal("BB_DESVIACION", "BB_DESVIACION", 2.0);
  setVal("VENTANA_AGOTAMIENTO", "VENTANA_AGOTAMIENTO", 3);
  setVal("EMA_RAPIDA", "EMA_RAPIDA", 20);
  setVal("EMA_LENTA", "EMA_LENTA", 50);
  setVal("SMA_TENDENCIA", "SMA_TENDENCIA", 200);
  setVal("STOCH_SOBREVENTA", "STOCH_SOBREVENTA", 20.0);
  setVal("STOCH_SOBRECOMPRA", "STOCH_SOBRECOMPRA", 80.0);
  setVal("ATR_PERIODO", "ATR_PERIODO", 14);
  dlg.showModal();
}

function closeIndModal() {
  const dlg = $("dlgIndParams");
  if (dlg && dlg.open) dlg.close();
}

async function saveIndModal() {
  const getNum = (id, fallback) => {
    const el = $("m_" + id);
    return el && el.value !== "" ? Number(el.value) : fallback;
  };
  const rsiPer = getNum("RSI_PERIODO", 14);
  const rsiOv = getNum("RSI_SOBREVENTA", 35.0);
  const rsiOb = getNum("RSI_SOBRECOMPRA", 65.0);
  if (rsiOv >= rsiOb) {
    toast("El umbral de sobreventa debe ser menor al de sobrecompra.", "err");
    return;
  }
  const vals = {
    RSI_PERIODO: rsiPer,
    RSI_SOBREVENTA: rsiOv,
    RSI_SOBRECOMPRA: rsiOb,
    BB_PERIODO: getNum("BB_PERIODO", 20),
    BB_DESVIACION: getNum("BB_DESVIACION", 2.0),
    VENTANA_AGOTAMIENTO: getNum("VENTANA_AGOTAMIENTO", 3),
    EMA_RAPIDA: getNum("EMA_RAPIDA", 20),
    EMA_LENTA: getNum("EMA_LENTA", 50),
    SMA_TENDENCIA: getNum("SMA_TENDENCIA", 200),
    STOCH_SOBREVENTA: getNum("STOCH_SOBREVENTA", 20.0),
    STOCH_SOBRECOMPRA: getNum("STOCH_SOBRECOMPRA", 80.0),
    ATR_PERIODO: getNum("ATR_PERIODO", 14),
  };
  const r = await applyConfig(vals);
  if (r && r.ok) {
    closeIndModal();
    updateIndicatorPills(cfgValues);
    refreshChart(true);
  }
}

function resetModalDefaults() {
  const setVal = (id, def) => { const el = $("m_" + id); if (el) el.value = def; };
  setVal("RSI_PERIODO", 14);
  setVal("RSI_SOBREVENTA", 35.0);
  setVal("RSI_SOBRECOMPRA", 65.0);
  setVal("BB_PERIODO", 20);
  setVal("BB_DESVIACION", 2.0);
  setVal("VENTANA_AGOTAMIENTO", 3);
  setVal("EMA_RAPIDA", 20);
  setVal("EMA_LENTA", 50);
  setVal("SMA_TENDENCIA", 200);
  setVal("STOCH_SOBREVENTA", 20.0);
  setVal("STOCH_SOBRECOMPRA", 80.0);
  setVal("ATR_PERIODO", 14);
}

if ($("btnOpenIndModal")) $("btnOpenIndModal").onclick = openIndModal;
if ($("btnCloseIndModal")) $("btnCloseIndModal").onclick = closeIndModal;
if ($("btnCancelIndModal")) $("btnCancelIndModal").onclick = closeIndModal;
if ($("btnSaveIndModal")) $("btnSaveIndModal").onclick = saveIndModal;
if ($("btnModalResetDefaults")) $("btnModalResetDefaults").onclick = resetModalDefaults;

const dlgInd = $("dlgIndParams");
if (dlgInd) {
  dlgInd.addEventListener("click", (e) => {
    const rect = dlgInd.getBoundingClientRect();
    const isInDialog = (rect.top <= e.clientY && e.clientY <= rect.top + rect.height
      && rect.left <= e.clientX && e.clientX <= rect.left + rect.width);
    if (!isInDialog) dlgInd.close();
  });
}

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
    desc: "Define qué reglas matemáticas e indicadores se evalúan para generar entradas (ej. Estrategia 1: Tendencia 12h/1d + retroceso RSI en sobreventa y toque de Banda Bollinger inferior en 15m/30m para compra alcista)."
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
  btLev: {
    title: "Apalancamiento (1x a 125x)",
    desc: "Multiplicador de tu capital. Margen × Apalancamiento define la posición nominal en Binance (debe ser de al menos $100 USDT para BTC/USDT)."
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

function updateBtNotional() {
  const mInput = $("btMargen") ? $("btMargen").value.trim() : "";
  const lInput = $("btLev") ? $("btLev").value.trim() : "";
  const m = parseFloat(mInput.replace(",", ".")) || (mInput === "0" ? 0 : 20);
  const l = parseInt(lInput.replace(",", ".")) || (lInput === "0" ? 0 : 10);
  const nom = m * l;
  const bar = $("btNotionalBar");
  if (!bar) return;
  const valEl = $("btNotionalVal");
  const ruleEl = $("btNotionalRule");
  valEl.textContent = "$" + nom.toFixed(2) + " USDT";
  if (nom < 100) {
    bar.className = "nominal-calc-bar warn";
    ruleEl.textContent = `(⚠ Menor a $100 Binance. Sube el margen a $${(100 / Math.max(l, 1)).toFixed(1)} o el apalancamiento a ${Math.ceil(100 / Math.max(m, 0.1))}x)`;
  } else {
    bar.className = "nominal-calc-bar ok";
    ruleEl.textContent = `(✔ Cumple el mínimo obligatorio de Binance de $100 USDT)`;
  }
}
if ($("btMargen")) $("btMargen").addEventListener("input", updateBtNotional);
if ($("btLev")) $("btLev").addEventListener("input", updateBtNotional);

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
  updateBtNotional();
}

$("btnBtRun").onclick = async () => {
  const pMsg = $("btMsg");
  const pBar = $("btBar");
  const progBox = $("btProg");
  
  // Sanitizar inputs (permitir comas en números en teclado español)
  const slipVal = ($("btSlip").value || "0.0002").replace(",", ".").trim();
  const margenVal = ($("btMargen").value || "").replace(",", ".").trim();
  const levVal = ($("btLev").value || "").replace(",", ".").trim();
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
    apalancamiento: levVal,
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
initTfControls(); loadConfig(); poll(); refreshChart(true); initParamTips();
setInterval(poll, 2000); setInterval(refreshChart, 3000);


