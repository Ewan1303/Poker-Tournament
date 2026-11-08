"use strict";

// The table page. The server (webapp.py) does all the poker; this file draws
// whatever /api/state says and sends your decisions back.

const SUITS = { s: "♠", h: "♥", d: "♦", c: "♣" };
const HAND_PHRASES = {
  "High card": "high card", Pair: "a pair", "Two pair": "two pair", "Three of a kind": "three of a kind",
  Straight: "a straight", Flush: "a flush", "Full house": "a full house", "Four of a kind": "four of a kind",
  "Straight flush": "a straight flush",
};

const $ = (selector) => document.querySelector(selector);
const fmt = (n) => Math.round(n).toLocaleString("en-US");
const signed = (n) => (n > 0 ? "+" : n < 0 ? "−" : "") + fmt(Math.abs(n));
const tone = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "dim");
const esc = (text) => String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function ordinal(place) {
  if (place !== Math.floor(place)) return `joint ${place}`;
  const tens = place % 100;
  const suffix = tens >= 11 && tens <= 13 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[place % 10] || "th";
  return place + suffix;
}

let state = null; // the latest snapshot
let session = "";
let version = -1;
let seatLayout = "";
let decisionShown;
let lastLogId = 0;
let botList = null;
let view = "table";
let selectedRun = null;
let refreshTimer = null;

async function api(path, body) {
  const options = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

// -- keeping up with the table ---------------------------------------------

async function poll() {
  for (;;) {
    try {
      const snap = await api(`/api/state?since=${version}&session=${session}`);
      if (snap.session !== session) {
        session = snap.session;
        resetTable();
      }
      version = snap.version;
      state = snap;
      render(snap);
    } catch (error) {
      $("#status-line").textContent = "Reconnecting… is poker still running in your terminal?";
      await sleep(1500);
    }
  }
}

function resetTable() {
  seatLayout = "";
  decisionShown = undefined;
  lastLogId = 0;
  $("#log").innerHTML = "";
}

function render(snap) {
  $("#status-line").innerHTML = statusLine(snap);
  renderSeats(snap);
  setHTML($("#board"), [0, 1, 2, 3, 4].map((i) => card(snap.board[i])).join(""));
  $("#pot").textContent = snap.pot ? `Pot ${fmt(snap.pot)}` : "";
  const message = $("#message");
  message.textContent = tableMessage(snap);
  message.classList.toggle("you", snap.status === "your_turn");
  renderActions(snap);
  renderPlayback(snap);
  renderScoreboard(snap);
  renderComparison(snap);
  renderLog(snap);
}

function setHTML(element, html) {
  // Only touch the DOM when something changed, so cards don't flicker.
  if (element.dataset.html !== html) {
    element.innerHTML = html;
    element.dataset.html = html;
  }
}

function card(code, extra = "") {
  if (!code) return `<div class="card slot ${extra}"></div>`;
  if (code === "back") return `<div class="card back ${extra}"></div>`;
  const red = code[1] === "h" || code[1] === "d";
  return `<div class="card ${red ? "red" : ""} ${extra}"><span class="r">${code[0] === "T" ? "10" : code[0]}</span><span class="s">${SUITS[code[1]]}</span></div>`;
}

function statusLine(snap) {
  const parts = [snap.mode === "play" ? "<b>Playing</b>" : "<b>Watching</b>", snap.format === "cash" ? "cash game" : "sit-and-go"];
  if (snap.hand_no) parts.push(`hand ${fmt(snap.hand_no)}`);
  parts.push(`blinds ${fmt(snap.blinds[0])}/${fmt(snap.blinds[1])}`);
  if (snap.street && snap.status !== "hand_over" && snap.status !== "finished") parts.push(snap.street);
  if (snap.paused) parts.push('<b class="live">paused</b>');
  return parts.join(" · ");
}

function tableMessage(snap) {
  if (snap.status === "loading") return "Loading the bots…";
  if (snap.status === "error") return "Something went wrong: see the hand log.";
  if (snap.status === "finished") {
    const winner = snap.scoreboard.rows.find((row) => row.place === 1);
    if (!winner) return "Game over";
    return winner.you ? "You won the sit-and-go!" : `${winner.name} wins the sit-and-go!`;
  }
  if (snap.status === "your_turn") return snap.you.to_call ? `Your turn: ${fmt(snap.you.to_call)} to call` : "Your turn";
  if (snap.status === "hand_over" && snap.result) {
    return snap.result.winners
      .map((w) => `${w.name} ${w.name.toLowerCase() === "you" ? "win" : "wins"} ${fmt(w.amount)}${w.hand_name ? ` with ${HAND_PHRASES[w.hand_name]}` : ""}`)
      .join(" · ");
  }
  const turn = snap.seats.find((seat) => seat.turn);
  return turn ? `${turn.name} to act` : "";
}

// -- seats -----------------------------------------------------------------

function around(index, count, rx, ry) {
  // Seat 0 (or yours) at the bottom, the rest clockwise, as the deal goes.
  const angle = Math.PI / 2 + (2 * Math.PI * index) / count;
  return { x: 50 + rx * Math.cos(angle), y: 50 + ry * Math.sin(angle) };
}

function layoutSeats(snap) {
  const key = `${snap.session}:${snap.seats.length}`;
  if (key === seatLayout) return;
  seatLayout = key;
  const n = snap.seats.length;
  const bottom = snap.your_seat ?? 0;
  const spot = (seat, rx, ry) => around((seat - bottom + n) % n, n, rx, ry);
  $("#seats").innerHTML = snap.seats.map((seat) => {
    const p = spot(seat.seat, 44, 40);
    return `<div class="seat" data-seat="${seat.seat}" style="left:${p.x}%;top:${p.y}%">
      <div class="hole"></div>
      <div class="plate"><span class="name"></span><span class="stack"></span><div class="dealer">D</div></div>
      <div class="bubble"></div>
      <div class="badge"></div>
    </div>`;
  }).join("");
  $("#bets").innerHTML = snap.seats.map((seat) => {
    const p = spot(seat.seat, 27, 21);
    return `<div class="bet" data-seat="${seat.seat}" style="left:${p.x}%;top:${p.y}%" hidden></div>`;
  }).join("");
}

function renderSeats(snap) {
  layoutSeats(snap);
  const winners = new Set((snap.result?.winners || []).map((w) => w.seat));
  const over = snap.status === "hand_over" || snap.status === "finished";
  for (const seat of snap.seats) {
    const el = document.querySelector(`.seat[data-seat="${seat.seat}"]`);
    const out = seat.place != null && seat.stack === 0;
    el.className = [
      "seat",
      seat.you && "you",
      seat.turn && !over && "turn",
      seat.folded && "folded",
      seat.all_in && "all-in",
      seat.button && "button",
      out && "out",
      over && winners.has(seat.seat) && "winner",
    ].filter(Boolean).join(" ");
    el.title = seat.description ? `${seat.name}${seat.style ? ` · ${seat.style}` : ""}\n${seat.description}` : seat.name;
    setHTML(el.querySelector(".hole"), (seat.cards || []).map((code) => card(code)).join(""));
    const warn = seat.errors ? ` <span class="neg" title="errors or timeouts">⚠ ${seat.errors}</span>` : "";
    const tag = seat.you && seat.name.toLowerCase() !== "you" ? '<span class="tag">you</span>' : "";
    setHTML(el.querySelector(".name"), esc(seat.name) + tag + warn);
    el.querySelector(".stack").textContent = out ? `Out · ${ordinal(seat.place)}` : fmt(seat.stack);
    const bubble = el.querySelector(".bubble");
    bubble.textContent = seat.action || "";
    bubble.className = "bubble" + (seat.action === "Fold" ? " fold" : /^(Raise|Bet)/.test(seat.action || "") ? " raise" : "");
    const badge = el.querySelector(".badge");
    if (over && winners.has(seat.seat)) {
      badge.innerHTML = `+${fmt(seat.collected)}${seat.hand_name ? ` · ${esc(seat.hand_name)}` : ""}`;
      badge.className = "badge";
    } else {
      badge.textContent = over && seat.hand_name ? seat.hand_name : "";
      badge.className = "badge dim";
    }
    const bet = document.querySelector(`.bet[data-seat="${seat.seat}"]`);
    bet.hidden = !seat.bet;
    if (seat.bet) setHTML(bet, `<span class="chip"></span>${fmt(seat.bet)}`);
  }
}

// -- your decisions ----------------------------------------------------------

function potSized(you, fraction) {
  const target = you.current_bet + Math.floor(fraction * (you.pot + you.to_call));
  return Math.max(you.min_raise_to, Math.min(you.max_raise_to, target));
}

function renderActions(snap) {
  const box = $("#actions");
  if (snap.mode !== "play") {
    box.innerHTML = "";
    decisionShown = undefined;
    return;
  }
  const you = snap.you;
  const id = you ? you.decision : null;
  if (id === decisionShown) return; // leave the slider where you put it
  decisionShown = id;
  if (!you) {
    box.className = "actions waiting";
    box.innerHTML = `<div class="action-row">
      <button class="btn act fold" disabled>Fold</button><button class="btn act call" disabled>Check / call</button><button class="btn act raise" disabled>Bet / raise</button>
    </div>`;
    return;
  }
  box.className = "actions";
  const verb = you.current_bet ? "Raise to" : "Bet";
  let html = "";
  if (you.can_raise) {
    const presets = [["Min", you.min_raise_to], ["½ pot", potSized(you, 0.5)], ["¾ pot", potSized(you, 0.75)], ["Pot", potSized(you, 1)], ["All in", you.max_raise_to]];
    const step = Math.max(1, Math.floor(you.big_blind / 2));
    html += `<div class="action-row">
      ${presets.map(([label, value]) => `<button class="btn preset" data-amount="${value}">${label}</button>`).join("")}
      <input type="range" id="raise-range" min="${you.min_raise_to}" max="${you.max_raise_to}" step="${step}" value="${you.min_raise_to}">
      <input type="number" class="amount" id="raise-amount" min="${you.min_raise_to}" max="${you.max_raise_to}" value="${you.min_raise_to}">
    </div>`;
  }
  html += `<div class="action-row">
    ${you.to_call ? '<button class="btn act fold" data-act="fold">Fold<span class="key">F</span></button>' : ""}
    <button class="btn act call" data-act="call">${you.to_call ? `Call ${fmt(you.to_call)}` : "Check"}<span class="key">C</span></button>
    ${you.can_raise ? `<button class="btn act raise" data-act="raise" id="raise-button">${verb} ${fmt(you.min_raise_to)}<span class="key">R</span></button>` : ""}
  </div>`;
  box.innerHTML = html;

  const range = $("#raise-range");
  const amount = $("#raise-amount");
  const setAmount = (value) => {
    const clamped = Math.max(you.min_raise_to, Math.min(you.max_raise_to, Math.round(Number(value) || 0)));
    if (range) range.value = clamped;
    if (amount && document.activeElement !== amount) amount.value = clamped;
    const button = $("#raise-button");
    if (button) button.innerHTML = `${clamped === you.max_raise_to ? "All in" : verb} ${fmt(clamped)}<span class="key">R</span>`;
    return clamped;
  };
  range?.addEventListener("input", () => setAmount(range.value));
  amount?.addEventListener("input", () => setAmount(amount.value));
  box.querySelectorAll(".preset").forEach((button) => button.addEventListener("click", () => setAmount(button.dataset.amount)));
  box.querySelectorAll("[data-act]").forEach((button) => button.addEventListener("click", () => decide(button.dataset.act)));
}

async function decide(type) {
  if (!state?.you) return;
  const body = { type };
  if (type === "raise") {
    const amount = $("#raise-amount");
    body.amount = Math.max(state.you.min_raise_to, Math.min(state.you.max_raise_to, Math.round(Number(amount?.value) || state.you.min_raise_to)));
  }
  $("#actions").querySelectorAll("button, input").forEach((el) => { el.disabled = true; });
  try {
    await api("/api/action", body);
  } catch (error) {
    $("#message").textContent = error.message;
  }
}

// -- watching controls -----------------------------------------------------------

function renderPlayback(snap) {
  $("#pause").textContent = snap.paused ? "Resume" : "Pause";
  $("#step").disabled = !snap.paused;
  const speed = $("#speed");
  if (document.activeElement !== speed) speed.value = snap.speed;
  $("#show-cards-wrap").hidden = snap.mode !== "watch";
  $("#show-cards").checked = snap.show_cards;
}

async function control(body) {
  try {
    await api("/api/control", body);
  } catch (error) {
    $("#message").textContent = error.message;
  }
}

$("#pause").addEventListener("click", () => control({ paused: !state?.paused }));
$("#step").addEventListener("click", () => control({ step: true }));
$("#next").addEventListener("click", () => control({ next_hand: true }));
$("#speed").addEventListener("change", (event) => control({ speed: event.target.value }));
$("#show-cards").addEventListener("change", (event) => control({ show_cards: event.target.checked }));

document.addEventListener("keydown", (event) => {
  if ($("#setup").open || view !== "table" || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.id === "raise-amount" && event.key === "Enter") {
    event.preventDefault();
    decide("raise");
    return;
  }
  if (event.target.matches("input, select, textarea")) return;
  const key = event.key.toLowerCase();
  const you = state?.you;
  if (you && key === "f" && you.to_call) decide("fold");
  else if (you && key === "c") decide("call");
  else if (you && key === "r" && you.can_raise) {
    event.preventDefault();
    $("#raise-amount")?.select();
  } else if (key === "p") control({ paused: !state?.paused });
  else if (key === "n") control({ next_hand: true });
  else if (key === "s" && state?.paused) control({ step: true });
});

// -- side panels ---------------------------------------------------------------

function who(snap, row) {
  // A player's name, with their style underneath and their description on hover.
  const seat = snap.seats[row.seat] || {};
  const style = seat.style ? `<small>${esc(seat.style)}</small>` : "";
  return `<td class="who" title="${esc(seat.description || "")}">${esc(row.name)}${style}</td>`;
}

function renderScoreboard(snap) {
  const board = snap.scoreboard;
  let html;
  if (snap.format === "cash") {
    $("#score-title").textContent = `Scoreboard · ${fmt(board.hands)} hand${board.hands === 1 ? "" : "s"}`;
    html = `<table class="score"><tr><th></th><th class="who">Player</th><th>Chips</th><th title="Big blinds won per 100 hands">bb/100</th></tr>`;
    board.rows.forEach((row, i) => {
      html += `<tr class="${row.you ? "you" : ""}"><td>${i + 1}</td>${who(snap, row)}
        <td class="${tone(row.total)}">${signed(row.total)}</td><td class="${tone(row.bb_per_100)}">${row.bb_per_100.toFixed(1)}</td></tr>`;
    });
  } else {
    $("#score-title").textContent = "Chip counts";
    html = `<table class="score"><tr><th></th><th class="who">Player</th><th>Chips</th></tr>`;
    board.rows.forEach((row, i) => {
      const chips = row.place != null ? `<span class="dim">${row.place === 1 ? "Winner" : `Out · ${ordinal(row.place)}`}</span>` : fmt(row.stack);
      html += `<tr class="${row.you ? "you" : ""}"><td>${i + 1}</td>${who(snap, row)}<td>${chips}</td></tr>`;
    });
  }
  setHTML($("#scoreboard"), html + "</table>");
}

function renderComparison(snap) {
  const panel = $("#compare-panel");
  const comparison = snap.comparison;
  panel.hidden = !comparison;
  if (!comparison) return;
  let html = `<table class="score"><tr><th></th><th class="who">In your seat</th><th>Chips</th><th>bb/100</th><th>Last hand</th></tr>`;
  comparison.rows.forEach((row, i) => {
    const last = row.last == null ? '<span class="dim">…</span>' : `<span class="${tone(row.last)}">${signed(row.last)}</span>`;
    html += `<tr class="${row.you ? "you" : ""}"><td>${i + 1}</td><td class="who">${esc(row.name)}</td>
      <td class="${tone(row.total)}">${signed(row.total)}</td><td class="${tone(row.bb_per_100)}">${row.bb_per_100.toFixed(1)}</td><td>${last}</td></tr>`;
  });
  setHTML($("#comparison"), html + "</table>");
}

function renderLog(snap) {
  const box = $("#log");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const fresh = snap.log.filter((entry) => entry.id > lastLogId);
  for (const entry of fresh) {
    const row = document.createElement("div");
    row.className = `entry ${entry.kind}`;
    row.innerHTML = esc(entry.text) + (entry.cards ? " " + entry.cards.map((code) => card(code)).join("") : "");
    box.appendChild(row);
    lastLogId = entry.id;
  }
  if (snap.error && !box.querySelector(".crash")) {
    const row = document.createElement("pre");
    row.className = "entry error crash";
    row.textContent = snap.error;
    box.appendChild(row);
  }
  while (box.children.length > 300) box.firstChild.remove();
  if (fresh.length && atBottom) box.scrollTop = box.scrollHeight;
}

// -- new game dialog -------------------------------------------------------------

const setup = $("#setup");
const form = $("#setup-form");

function pick(name, bot, checked) {
  return `<label class="bot-pick" title="${esc(bot.description)}"><input type="checkbox" name="${name}" value="${esc(bot.name)}" ${checked ? "checked" : ""}>
    ${esc(bot.display)}${bot.builtin ? "" : " <small>yours</small>"}</label>`;
}

function botCard(bot, checked) {
  return `<label class="bot-card"><input type="checkbox" name="bots" value="${esc(bot.name)}" ${checked ? "checked" : ""}>
    <span><b>${esc(bot.display)}</b>${bot.style ? `<em>${esc(bot.style)}</em>` : ""}${bot.builtin ? "" : '<em class="yours">yours</em>'}
    ${bot.description ? `<small>${esc(bot.description)}</small>` : ""}<code>${esc(bot.name)}</code></span></label>`;
}

async function openSetup() {
  if (!botList) botList = (await api("/api/bots")).bots;
  const config = state?.config || {};
  form.elements.mode.value = config.mode || "play";
  form.elements.format.value = config.format || "cash";
  $("#bot-list").innerHTML = botList.map((bot) => botCard(bot, (config.bots || []).includes(bot.name))).join("");
  $("#compare-list").innerHTML = botList.map((bot) => pick("compare", bot, (config.compare || []).includes(bot.name))).join("");
  for (const key of ["player_name", "stack", "small_blind", "big_blind", "hands_per_level"]) {
    form.elements[key].value = config[key] ?? "";
  }
  $("#setup-error").textContent = "";
  updateSetup();
  setup.showModal();
}

function updateSetup() {
  const play = form.elements.mode.value === "play";
  const sng = form.elements.format.value === "sng";
  form.querySelectorAll(".play-only").forEach((el) => { el.hidden = !play; });
  form.querySelectorAll(".compare-only").forEach((el) => { el.hidden = !play || sng; });
  form.querySelectorAll(".sng-only").forEach((el) => { el.hidden = !sng; });
  form.elements.player_name.required = play;
  const seats = form.querySelectorAll('input[name="bots"]:checked').length + (play ? 1 : 0);
  $("#seat-count").textContent = `· ${seats} of 10 seats${play ? ", including you" : ""}`;
}

form.addEventListener("change", updateSetup);
$("#setup-cancel").addEventListener("click", () => setup.close());
$("#new-game").addEventListener("click", () => openSetup().catch((error) => alert(error.message)));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  const play = data.get("mode") === "play";
  const body = {
    mode: data.get("mode"),
    format: data.get("format"),
    bots: data.getAll("bots"),
    compare: play && data.get("format") === "cash" ? data.getAll("compare") : [],
    player_name: data.get("player_name") || "You",
    stack: Number(data.get("stack")),
    small_blind: Number(data.get("small_blind")),
    big_blind: Number(data.get("big_blind")),
    hands_per_level: Number(data.get("hands_per_level")) || 10,
    speed: state?.speed || "normal",
  };
  try {
    await api("/api/start", body);
    setup.close();
    switchView("table");
  } catch (error) {
    $("#setup-error").textContent = error.message;
  }
});

// -- results ---------------------------------------------------------------------

function switchView(name) {
  view = name;
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
  $("#table-view").hidden = name !== "table";
  $("#results-view").hidden = name !== "results";
  history.replaceState(null, "", name === "results" ? "#results" : "#");
  clearTimeout(refreshTimer);
  if (name === "results") loadRuns();
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.view)));

async function loadRuns() {
  let runs;
  try {
    runs = (await api("/api/results")).runs;
  } catch (error) {
    $("#runs").innerHTML = `<p class="empty">${esc(error.message)}</p>`;
    return;
  }
  if (!runs.length) {
    $("#runs").innerHTML = "";
    $("#run").innerHTML = `<p class="empty">No tournament results yet.<br>Run one with <code>poker run bots</code> and it shows up here.</p>`;
    return;
  }
  if (!runs.some((run) => run.name === selectedRun)) selectedRun = runs[0].name;
  $("#runs").innerHTML = runs.map((run) => `<button class="run-item ${run.name === selectedRun ? "active" : ""}" data-run="${esc(run.name)}">
      ${esc(run.name)}<small>${run.done ? "" : '<span class="live">in progress · </span>'}${new Date(run.modified * 1000).toLocaleString()}</small>
    </button>`).join("");
  $("#runs").querySelectorAll(".run-item").forEach((item) => item.addEventListener("click", () => {
    selectedRun = item.dataset.run;
    $("#runs").querySelectorAll(".run-item").forEach((other) => other.classList.toggle("active", other === item));
    showRun(selectedRun);
  }));
  showRun(selectedRun);
}

async function showRun(name) {
  clearTimeout(refreshTimer);
  try {
    renderRun(await api(`/api/results/${encodeURIComponent(name)}`));
  } catch (error) {
    $("#run").innerHTML = `<p class="empty">${esc(error.message)}</p>`;
  }
  if (view === "results") refreshTimer = setTimeout(() => showRun(name), 5000);
}

function progress(done, total, what) {
  if (done >= total) return "";
  return `<div class="progress"><div style="width:${(100 * done) / Math.max(total, 1)}%"></div></div><p class="sub">${done} of ${total} ${what} finished so far</p>`;
}

function renderRun(run) {
  const config = run.config;
  let html = `<h1>${esc(run.name)}</h1><p class="sub">${run.done ? "Finished" : '<span class="live">In progress</span>'} · ${run.bots.length} bots${run.created ? ` · ${new Date(run.created).toLocaleString()}` : ""}</p>`;
  const errors = [];
  if (run.heads_up) {
    const board = run.heads_up.leaderboard;
    html += `<h3>Heads-up round robin</h3><p class="sub">${fmt(config.hands)} deals per pairing, each played from both seats · ${fmt(config.stack / config.big_blind)} big blinds deep · bars show bb/100, lines the 95% range</p>`;
    if (run.heads_up.progress) html += progress(...run.heads_up.progress, "pairings");
    if (board.length) {
      const scale = Math.max(1, ...board.map((row) => Math.abs(row.bb_per_100) + 1.96 * row.stderr));
      const at = (value) => 50 + (50 * value) / scale;
      html += `<div class="bars"><span></span><span class="head">Bot</span><span class="head">bb/100</span><span class="head" style="text-align:right">Score</span><span class="head" style="text-align:right">Won-lost</span>`;
      board.forEach((row, i) => {
        const end = at(row.bb_per_100);
        const ci = 1.96 * row.stderr;
        html += `<span class="rank">${i + 1}</span><span class="name" title="${esc(row.bot)}">${esc(row.bot)}</span>
          <div class="track"><div class="zero"></div>
            <div class="fill ${row.bb_per_100 >= 0 ? "pos" : "neg"}" style="left:${Math.min(50, end)}%;width:${Math.abs(end - 50)}%"></div>
            <div class="ci" style="left:${at(row.bb_per_100 - ci)}%;width:${at(row.bb_per_100 + ci) - at(row.bb_per_100 - ci)}%"></div>
          </div>
          <span class="val ${tone(row.bb_per_100)}">${row.bb_per_100 >= 0 ? "+" : "−"}${Math.abs(row.bb_per_100).toFixed(1)} <span class="dim">± ${ci.toFixed(1)}</span></span>
          <span class="record">${row.won}-${row.lost}</span>`;
        for (const error of row.errors || []) errors.push(`${row.bot}: ${error.trim().split("\n").pop()}`);
      });
      html += `</div>`;

      const names = board.map((row) => row.bot);
      const values = names.flatMap((a) => names.map((b) => run.heads_up.head_to_head[a]?.[b]?.bb_per_100).filter((v) => v != null));
      const strongest = Math.max(1, ...values.map(Math.abs));
      html += `<h3>Head to head</h3><p class="sub">bb/100 won by the bot on the left against the bot on top</p><div style="overflow-x:auto"><table class="grid"><tr><th></th>${names.map((n) => `<th title="${esc(n)}">${esc(n)}</th>`).join("")}</tr>`;
      for (const a of names) {
        html += `<tr><th class="row">${esc(a)}</th>`;
        for (const b of names) {
          const cell = run.heads_up.head_to_head[a]?.[b];
          if (a === b) html += `<td class="self">—</td>`;
          else if (!cell) html += `<td class="self">·</td>`;
          else {
            const alpha = 0.15 + 0.65 * Math.min(1, Math.abs(cell.bb_per_100) / strongest);
            const color = cell.bb_per_100 >= 0 ? `rgba(63,191,111,${alpha})` : `rgba(240,96,96,${alpha})`;
            html += `<td style="background:${color}" title="${esc(a)} vs ${esc(b)}: ±${(1.96 * cell.stderr).toFixed(1)}">${cell.bb_per_100 >= 0 ? "+" : "−"}${Math.abs(cell.bb_per_100).toFixed(0)}</td>`;
          }
        }
        html += `</tr>`;
      }
      html += `</table></div>`;
    }
  }
  if (run.sng) {
    html += `<h3>Sit-and-go</h3><p class="sub">Points: 1 for a win down to 0 for last place</p>`;
    if (run.sng.progress) html += progress(...run.sng.progress, "games");
    html += `<table class="plain"><tr><th>#</th><th>Bot</th><th>Avg points</th><th>Wins</th><th>Games</th><th>Avg place</th></tr>`;
    run.sng.leaderboard.forEach((row, i) => {
      html += `<tr><td>${i + 1}</td><td>${esc(row.bot)}</td><td>${row.avg_points.toFixed(3)}</td><td>${row.wins}</td><td>${row.games}</td><td>${row.avg_place.toFixed(2)}</td></tr>`;
      for (const error of row.errors || []) errors.push(`${row.bot}: ${error.trim().split("\n").pop()}`);
    });
    html += `</table>`;
  }
  if (errors.length) html += `<h3>Bot errors</h3>${[...new Set(errors)].map((e) => `<p class="errors">${esc(e)}</p>`).join("")}`;
  $("#run").innerHTML = html;
}

// -- start --------------------------------------------------------------------------

if (location.hash === "#results") switchView("results");
poll();
