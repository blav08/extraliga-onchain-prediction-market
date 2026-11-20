/*
 * Extraliga Prediction Market frontend. Plain JS + ethers v6, no build step.
 *
 * Data flow:
 *   board.json (written by the oracle every 30 min) -> which games exist, teams, logos, scores
 *   the contract itself (read live)                -> pools, status, your stakes, what you can claim
 * The chain is always the source of truth; board.json only adds the human-readable context.
 */
"use strict";

const CONFIG = {
  chainId: 11155111,
  chainHex: "0xaa36a7",
  rpc: "https://ethereum-sepolia-rpc.publicnode.com",
  explorer: "https://eth-sepolia.blockscout.com",
  token: "0xb459280925651b0a4ee6b1f71C88C87cc1787512",
  pool: "0x684C429c009088b2Ed69410d8e974aec5b4fa7Af",
  faucet: "0xA3e78CD63AE1F6C6D8b4E12B3730C725E561DC9E",
  repo: "https://github.com/blav08/extraliga-onchain-prediction-market",
  // raw.githubusercontent serves the oracle's latest commit with CORS enabled.
  // Override for local testing with ?board=path/to/board.json
  board: "https://raw.githubusercontent.com/blav08/extraliga-onchain-prediction-market/main/data/board.json",
  // The oracle's audit log: source of the dataset sold in the data shop
  log: "https://raw.githubusercontent.com/blav08/extraliga-onchain-prediction-market/main/data/oracle-log.jsonl",
  // Blockscout keeps the full event history (public RPC nodes only ~1.5 days)
  explorerApi: "https://eth-sepolia.blockscout.com/api",
};
// Test harnesses may inject overrides before this script runs. Deliberately NOT read from
// the URL: a crafted link must never point a user's wallet at a different contract.
Object.assign(CONFIG, window.__CONFIG_OVERRIDE__ || {});

// Human-readable ABIs: only what the page needs. Custom errors let ethers decode reverts.
const POOL_ABI = [
  "function getMatch(bytes32) view returns (uint64 startTime, uint8 status, uint8 winner, uint256 homePool, uint256 awayPool)",
  "function stakesOf(bytes32, address) view returns (uint256 home, uint256 away)",
  "function claimable(bytes32, address) view returns (uint256)",
  "function feeBps() view returns (uint16)",
  "function ORACLE_ROLE() view returns (bytes32)",
  "function hasRole(bytes32, address) view returns (bool)",
  "function placeBet(bytes32 matchId, uint8 side, uint256 amount)",
  "function claim(bytes32 matchId)",
  "error MatchNotOpen()", "error BettingClosed()", "error BetTooSmall()",
  "error NothingToClaim()", "error AlreadyClaimed()",
];
const TOKEN_ABI = [
  "function balanceOf(address) view returns (uint256)",
  "function allowance(address owner, address spender) view returns (uint256)",
  "function approve(address spender, uint256 value) returns (bool)",
  "function nonces(address) view returns (uint256)",
  "function name() view returns (string)",
  "function burnShareBps() view returns (uint16)",
  "function payForData(bytes32 datasetId, uint256 amount)",
  "event DataPurchased(address indexed buyer, bytes32 indexed datasetId, uint256 paid, uint256 burned)",
  "error ERC20InsufficientBalance(address sender, uint256 balance, uint256 needed)",
  "error WalletLimitExceeded(address account, uint256 balance, uint256 limit)",
  "error EnforcedPause()",
];
const FAUCET_ABI = [
  "function drip()",
  "function lastClaim(address) view returns (uint64)",
  "function dripAmount() view returns (uint256)",
  "error CooldownActive(uint64 nextClaimAt)",
];

const ERROR_TEXT = {
  BettingClosed: "Betting on this game closed at face-off.",
  MatchNotOpen: "This game is no longer open for bets.",
  BetTooSmall: "The minimum stake is 1 MATCH.",
  NothingToClaim: "There is nothing to collect on this game.",
  AlreadyClaimed: "You have already collected this game.",
  ERC20InsufficientBalance: "You don't have enough MATCH. Take free tokens from the faucet first.",
  WalletLimitExceeded: "This would put your wallet over the 2 % holding limit.",
  EnforcedPause: "Token transfers are paused by the admin right now.",
  CooldownActive: "You already took tokens in the last 24 hours.",
};

const STATUS = ["NONE", "OPEN", "SETTLED", "CANCELLED"];
const HOME = 0, AWAY = 1;
const ONE = 10n ** 18n;
// One-time spending allowance for the pool. Deliberately not "unlimited": MetaMask
// flags unlimited approvals, and 10,000 MATCH covers ~100 days of faucet drips.
const APPROVE_CAP = 10_000n * ONE;

/*
 * Data shop. The token's main use case: buying match data with MATCH.
 * One dataset for now: every game the oracle has settled or cancelled, with the
 * evidence it acted on and the transaction hash, rebuilt from the public audit log.
 * Paying calls MatchToken.payForData, which burns burnShareBps of the price and
 * sends the rest to the treasury; the DataPurchased event is the receipt.
 */
const DATASET = {
  id: ethers.id("dataset:extraliga-verified-results:2026-27"),  // what payForData records on chain
  title: "Verified Extraliga results 2026/27",
  price: 50n * ONE,
  file: "extraliga-verified-results.csv",
};

const state = {
  board: [], chain: new Map(), mine: new Map(), feeBps: 200n,
  account: null, chainOk: false, balance: 0n, eth: 0n, nextDrip: 0,
  connecting: false,
  allowance: 0n,             // how much MATCH the pool may pull from this wallet
  isOracle: false,           // connected account holds ORACLE_ROLE -> must not bet
  pick: new Map(),           // matchId -> 0 | 1, the side the user is staking on
  amount: new Map(),         // matchId -> typed stake, survives re-renders
  busy: false,
  shop: { rows: [], burnBps: 3000n, owned: false, receipt: null },
};

const read = new ethers.JsonRpcProvider(CONFIG.rpc, CONFIG.chainId, { staticNetwork: true });
const rPool = new ethers.Contract(CONFIG.pool, POOL_ABI, read);
const rToken = new ethers.Contract(CONFIG.token, TOKEN_ABI, read);
const rFaucet = new ethers.Contract(CONFIG.faucet, FAUCET_ABI, read);
const errIface = new ethers.Interface([...POOL_ABI, ...TOKEN_ABI, ...FAUCET_ABI].filter((s) => s.startsWith("error")));

// ------------------------------------------------------------------ helpers
const $ = (sel) => document.querySelector(sel);

/** Tiny element builder. Text always goes through textContent: board data is third-party. */
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "class") n.className = v;
    else if (k === "style") n.style.cssText = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return n;
}

function fmt(wei, digits = 1) {
  const n = Number(ethers.formatUnits(wei, 18));
  return n.toLocaleString("en-GB", { maximumFractionDigits: n < 10 ? 2 : digits });
}

const prague = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Prague", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});
const when = (ts) => prague.format(new Date(ts * 1000));

function until(ts) {
  const s = ts - Math.floor(Date.now() / 1000);
  if (s <= 0) return null;
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} d ${h} h`;
  if (h > 0) return `${h} h ${m} min`;
  return `${Math.max(m, 1)} min`;
}

function short(a) { return `${a.slice(0, 6)}…${a.slice(-4)}`; }

let toastTimer;
function toast(msg, { err = false, link = null, linkText = "View transaction", sticky = false } = {}) {
  const t = $("#toast");
  t.replaceChildren(msg, link ? el("span", {}, " ", el("a", { href: link, target: "_blank", rel: "noopener" }, linkText)) : "");
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  if (!sticky) toastTimer = setTimeout(() => (t.hidden = true), err ? 9000 : 6000);
}

function explain(e) {
  const code = e?.code ?? e?.info?.error?.code ?? e?.error?.code;
  if (code === "ACTION_REJECTED" || code === 4001) return "You cancelled the request in MetaMask.";
  // -32002: a request is already waiting in MetaMask; a second popup will not open
  if (code === -32002) return "MetaMask already has a request waiting. Click the fox icon in your browser toolbar and approve or reject it there.";
  if (code === 4100) return "MetaMask is locked or this site is not connected. Unlock MetaMask and click Connect again.";
  const data = e?.data ?? e?.info?.error?.data ?? e?.error?.data;
  let name = e?.revert?.name;
  if (!name && typeof data === "string") { try { name = errIface.parseError(data)?.name; } catch { /* unknown */ } }
  if (name && ERROR_TEXT[name]) return ERROR_TEXT[name];
  if (/insufficient funds/i.test(e?.message || "")) return "Not enough Sepolia ETH for gas. Get some from the public faucet linked below.";
  return e?.shortMessage || e?.message || "Something went wrong.";
}

/** Expected payout if the pool stayed as it is now. Same formula as the contract. */
function payout(stake, side, chainMatch) {
  if (stake <= 0n) return 0n;
  const pools = [chainMatch.homePool, chainMatch.awayPool];
  const total = pools[0] + pools[1] + stake;
  const distributable = total - (total * state.feeBps) / 10000n;
  return (stake * distributable) / (pools[side] + stake);
}

// ------------------------------------------------------------------ data loading
async function loadBoard() {
  // ?board= only accepts same-origin relative paths, for local development
  let override = new URLSearchParams(location.search).get("board");
  if (override && /^[a-z]+:|^\/\//i.test(override)) override = null;
  const url = override || `${CONFIG.board}?t=${Math.floor(Date.now() / 60000)}`; // bust cache once a minute
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`board.json: HTTP ${res.status}`);
  const data = await res.json();
  state.board = (data.matches || []).filter((m) => /^0x[0-9a-fA-F]{64}$/.test(m.match_id));
}

async function loadChain() {
  const ids = state.board.map((m) => m.match_id);
  const [fee, ...rows] = await Promise.all([rPool.feeBps(), ...ids.map((id) => rPool.getMatch(id))]);
  state.feeBps = BigInt(fee);
  rows.forEach((r, i) => state.chain.set(ids[i], {
    start: Number(r.startTime), status: STATUS[Number(r.status)], winner: Number(r.winner),
    homePool: r.homePool, awayPool: r.awayPool,
  }));
}

async function loadMine() {
  state.mine.clear();
  if (!state.account) return;
  const a = state.account;
  const ids = state.board.map((m) => m.match_id);
  const [bal, eth, last, allowance, isOracle, ...per] = await Promise.all([
    rToken.balanceOf(a), read.getBalance(a), rFaucet.lastClaim(a), rToken.allowance(a, CONFIG.pool),
    rPool.ORACLE_ROLE().then((role) => rPool.hasRole(role, a)),
    ...ids.flatMap((id) => [rPool.stakesOf(id, a), rPool.claimable(id, a)]),
  ]);
  state.balance = bal; state.eth = eth; state.isOracle = isOracle; state.allowance = allowance;
  state.nextDrip = Number(last) === 0 ? 0 : Number(last) + 86400;
  ids.forEach((id, i) => {
    const st = per[2 * i], cl = per[2 * i + 1];
    state.mine.set(id, { home: st.home, away: st.away, claimable: cl });
  });
}

/** Dataset rows from the oracle's audit log: the last successful settle/cancel per game. */
async function loadShop() {
  const [res, bps] = await Promise.all([
    fetch(`${CONFIG.log}?t=${Math.floor(Date.now() / 60000)}`, { cache: "no-store" }),
    rToken.burnShareBps(),
  ]);
  if (!res.ok) throw new Error(`oracle-log.jsonl: HTTP ${res.status}`);
  state.shop.burnBps = BigInt(bps);
  const last = new Map();
  for (const line of (await res.text()).split("\n")) {
    let e;
    try { e = JSON.parse(line); } catch { continue; }       // skip blank or broken lines
    if (e.dry_run || !e.ok || !["settle", "cancel"].includes(e.action)) continue;
    last.set(e.match_id, e);
  }
  state.shop.rows = [...last.values()].map((e) => {
    const [home, away] = String(e.match || "").split(" vs ");
    const ev = e.evidence || {};
    return {
      start: ev.start_ts, home, away, score: ev.score || [null, null], shootout: ev.shootout,
      winner: e.action === "settle" ? e.winner : null, action: e.action,
      provider: ev.provider, fixture: ev.fixture_id, matchId: e.match_id, tx: e.tx_hash,
    };
  }).sort((a, b) => b.start - a.start);                       // newest first
}

/** Has this wallet already paid for the dataset? Looks for its DataPurchased receipt. */
async function loadOwned() {
  state.shop.owned = false; state.shop.receipt = null;
  if (!state.account) return;
  const topic = (v) => ethers.zeroPadValue(v, 32).toLowerCase();
  const q = new URLSearchParams({
    module: "logs", action: "getLogs", fromBlock: "0", toBlock: "latest", address: CONFIG.token,
    topic0: ethers.id("DataPurchased(address,bytes32,uint256,uint256)"),
    topic1: topic(state.account), topic2: DATASET.id.toLowerCase(),
    topic0_1_opr: "and", topic0_2_opr: "and", topic1_2_opr: "and",
  });
  try {
    const body = await (await fetch(`${CONFIG.explorerApi}?${q}`)).json();
    const logs = Array.isArray(body.result) ? body.result : [];
    if (logs.length) { state.shop.owned = true; state.shop.receipt = logs[logs.length - 1].transactionHash; }
  } catch (e) { console.warn("[EPM] purchase lookup failed", e); }
  // The explorer indexes a few seconds behind the chain: remember a purchase made just now
  const local = sessionStorageGet(`bought:${state.account}:${DATASET.id}`);
  if (!state.shop.owned && local) { state.shop.owned = true; state.shop.receipt = local; }
}

function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch { return null; } }
function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private mode */ } }

async function refresh() {
  try {
    await loadBoard();
    await Promise.all([loadChain(), loadMine(), loadShop(), loadOwned()]);
    $("#notice").textContent = "";
    $("#notice").hidden = true;
  } catch (e) {
    console.error(e);
    $("#notice").textContent = `Could not load the latest data (${e.message}). Showing what is available; retrying in a minute.`;
    $("#notice").hidden = false;
  }
  render();
}

// ------------------------------------------------------------------ wallet
/*
 * Finding MetaMask. With several wallet extensions installed (Phantom, Coinbase,
 * Brave Wallet...) they fight over window.ethereum and the page may get the wrong one.
 * EIP-6963 fixes that: each wallet announces itself with an id ("rdns"), so we can
 * pick MetaMask explicitly. window.ethereum stays as a fallback for older wallets.
 */
const announced = new Map();     // rdns -> { info, provider }
window.addEventListener("eip6963:announceProvider", (e) => {
  if (e.detail?.info?.rdns && e.detail.provider) announced.set(e.detail.info.rdns, e.detail);
});
window.dispatchEvent(new Event("eip6963:requestProvider"));

let eth = null;                  // the EIP-1193 provider we talk to
function pickWallet() {
  const mm = announced.get("io.metamask") || announced.get("io.metamask.flask");
  if (mm) return mm.provider;
  if (announced.size) return [...announced.values()][0].provider;
  const injected = window.ethereum;
  if (injected?.providers?.length) return injected.providers.find((p) => p.isMetaMask) || injected.providers[0];
  return injected || null;
}
const isMobile = () => /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const metamaskDeepLink = () => `https://metamask.app.link/dapp/${location.host}${location.pathname}`;

function walletProvider() { return eth ? new ethers.BrowserProvider(eth) : null; }

async function connect() {
  eth = eth || pickWallet();
  if (!eth) {
    if (isMobile()) {
      toast("On a phone, open this page inside the MetaMask app.", { err: true, link: metamaskDeepLink(), linkText: "Open in MetaMask" });
    } else {
      toast("No wallet found in this browser. Install MetaMask from metamask.io, then reload the page.", { err: true });
    }
    return;
  }
  state.connecting = true; render();
  // MetaMask does not always open a window by itself (locked wallet, an earlier request
  // still open). Tell the user where to look instead of silently waiting.
  toast("Approve the connection in MetaMask. No window? Click the fox icon in your browser toolbar.", { sticky: true });
  try {
    const accounts = await eth.request({ method: "eth_requestAccounts" });
    if (!accounts?.length) throw new Error("MetaMask returned no account. Unlock it and try again.");
    state.account = ethers.getAddress(accounts[0]);
    await checkChain();
    $("#toast").hidden = true;
    try { await Promise.all([loadMine(), loadOwned()]); } catch (e) { console.error("[EPM] loading balances failed", e); }
  } catch (e) {
    console.error("[EPM] connect failed", e);
    toast(explain(e), { err: true });
  } finally {
    state.connecting = false;
    render();
  }
}

async function checkChain() {
  const id = await eth.request({ method: "eth_chainId" });
  state.chainOk = parseInt(id, 16) === CONFIG.chainId;
}

async function switchChain() {
  try {
    await eth.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CONFIG.chainHex }] });
  } catch (e) {
    if (e.code === 4902 || e?.data?.originalError?.code === 4902) {   // Sepolia not added to this wallet yet
      try {
        await eth.request({ method: "wallet_addEthereumChain", params: [{
          chainId: CONFIG.chainHex, chainName: "Sepolia", rpcUrls: [CONFIG.rpc],
          nativeCurrency: { name: "Sepolia ETH", symbol: "ETH", decimals: 18 }, blockExplorerUrls: [CONFIG.explorer],
        }] });
      } catch (e2) { toast(explain(e2), { err: true }); return; }
    } else { toast(explain(e), { err: true }); return; }
  }
  await checkChain();
  try { await loadMine(); } catch { /* shown on next refresh */ }
  render();
}

async function addTokenToWallet() {
  try {
    await eth.request({ method: "wallet_watchAsset", params: {
      type: "ERC20", options: { address: CONFIG.token, symbol: "MATCH", decimals: 18 },
    } });
  } catch (e) { toast(explain(e), { err: true }); }
}

/** Run a transaction with consistent feedback: wallet prompt -> submitted -> confirmed. */
async function send(label, fn) {
  if (state.busy) return;
  state.busy = true; render();
  try {
    toast("Confirm in MetaMask…", { sticky: true });
    const tx = await fn();
    const link = `${CONFIG.explorer}/tx/${tx.hash}`;
    toast(`${label}: waiting for the block…`, { link, sticky: true });
    const rc = await tx.wait();
    if (rc.status !== 1) throw new Error("Transaction failed on chain.");
    toast(`${label}: done.`, { link });
    await Promise.all([loadChain(), loadMine()]);
    return true;
  } catch (e) {
    console.error(e);
    toast(explain(e), { err: true });
    return false;
  } finally {
    state.busy = false; render();
  }
}

async function drip() {
  const signer = await walletProvider().getSigner();
  await send("Free tokens", () => new ethers.Contract(CONFIG.faucet, FAUCET_ABI, signer).drip());
}

/**
 * Betting = ERC-20 allowance + placeBet.
 * The first bet asks for a one-time allowance (approve), later bets are a single
 * transaction. We used EIP-2612 permit signatures before (one tx per bet), but wallet
 * security layers treat off-chain Permit signatures as the classic wallet-drainer
 * pattern, so every bet triggered several scary warnings. A plain approve to a
 * verified contract is the pattern wallets trust. The contract still supports
 * placeBetWithPermit for integrators.
 */
async function bet(matchId, side, amount) {
  const signer = await walletProvider().getSigner();
  const owner = await signer.getAddress();
  const token = new ethers.Contract(CONFIG.token, TOKEN_ABI, signer);
  const pool = new ethers.Contract(CONFIG.pool, POOL_ABI, signer);

  if ((await rToken.allowance(owner, CONFIG.pool)) < amount) {
    const ok = await send("Allow betting", () => {
      toast("Step 1 of 2: allow the betting pool to use your MATCH (one time only)…", { sticky: true });
      return token.approve(CONFIG.pool, amount > APPROVE_CAP ? amount : APPROVE_CAP);
    });
    if (!ok) return;
    // MetaMask lets the user edit the spending cap; make sure it still covers this bet
    if ((await rToken.allowance(owner, CONFIG.pool)) < amount) {
      toast("The spending cap you set in MetaMask is lower than this bet. Bet less, or allow a higher cap.", { err: true });
      return;
    }
  }
  const ok = await send("Bet", () => {
    toast("Confirm the bet in MetaMask…", { sticky: true });
    return pool.placeBet(matchId, side, amount);
  });
  if (ok) {
    state.pick.delete(matchId);
    state.amount.delete(matchId);
    render();
  }
}

async function claim(matchId) {
  const signer = await walletProvider().getSigner();
  await send("Collect", () => new ethers.Contract(CONFIG.pool, POOL_ABI, signer).claim(matchId));
}

/** Buy the dataset: one transaction, no approval needed (the token burns from the buyer itself). */
async function buyData() {
  const signer = await walletProvider().getSigner();
  const token = new ethers.Contract(CONFIG.token, TOKEN_ABI, signer);
  let hash = null;
  const ok = await send("Data purchase", async () => {
    const tx = await token.payForData(DATASET.id, DATASET.price);
    hash = tx.hash;
    return tx;
  });
  if (!ok) return;
  sessionStorageSet(`bought:${state.account}:${DATASET.id}`, hash);
  state.shop.owned = true; state.shop.receipt = hash;
  render();
  downloadCsv();
}

/** CSV file built in the browser from the same rows the preview shows. */
function downloadCsv() {
  const cell = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  const head = ["date_utc", "home", "away", "score_home", "score_away", "shootout", "winner",
    "oracle_action", "provider", "fixture_id", "match_id", "oracle_tx"];
  const lines = state.shop.rows.map((r) => [
    r.start ? new Date(r.start * 1000).toISOString().slice(0, 16).replace("T", " ") : "",
    r.home, r.away, r.score[0], r.score[1], r.shootout, r.winner, r.action, r.provider, r.fixture, r.matchId, r.tx,
  ].map(cell).join(","));
  const blob = new Blob([[head.join(","), ...lines].join("\n") + "\n"], { type: "text/csv" });
  const a = el("a", { href: URL.createObjectURL(blob), download: DATASET.file });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
}

// ------------------------------------------------------------------ rendering
function renderShop() {
  const box = $("#shop");
  if (!box) return;
  const { rows, burnBps, owned, receipt } = state.shop;
  const burned = (DATASET.price * burnBps) / 10000n;
  const score = (r) => r.score[0] == null ? "–" : `${r.score[0]}:${r.score[1]}${r.shootout ? " SO" : ""}`;
  const preview = el("table", { class: "sample" },
    el("thead", {}, el("tr", {}, el("th", {}, "Date"), el("th", {}, "Game"), el("th", {}, "Score"), el("th", {}, "Oracle tx"))),
    el("tbody", {}, rows.slice(0, 3).map((r) => el("tr", {},
      el("td", {}, r.start ? when(r.start) : ""),
      el("td", {}, `${r.home} – ${r.away}`),
      el("td", {}, score(r)),
      el("td", {}, r.tx ? el("a", { href: `${CONFIG.explorer}/tx/${r.tx}`, target: "_blank", rel: "noopener" }, short(r.tx)) : "")))));

  let action;
  if (!state.account) action = el("button", { class: "btn", type: "button", onclick: connect }, "Connect MetaMask to buy");
  else if (!state.chainOk) action = el("button", { class: "btn", type: "button", onclick: switchChain }, "Switch to Sepolia");
  else if (state.isOracle) action = el("span", { class: "hint warn" }, "The oracle account does not buy data.");
  else if (owned) action = el("div", { class: "owned" },
    el("button", { class: "btn", type: "button", onclick: downloadCsv, disabled: !rows.length }, "Download CSV"),
    el("span", { class: "hint" }, "Purchased. ", receipt ? el("a", { href: `${CONFIG.explorer}/tx/${receipt}`, target: "_blank", rel: "noopener" }, "See the burn on Blockscout") : ""));
  else {
    const short_ = state.balance < DATASET.price;
    action = el("div", { class: "owned" },
      el("button", { class: "btn", type: "button", onclick: buyData, disabled: state.busy || short_ || !rows.length },
        `Buy for ${fmt(DATASET.price)} MATCH`),
      short_ ? el("span", { class: "hint" }, "Not enough MATCH: take free tokens first.") : null);
  }

  box.replaceChildren(el("article", { class: "dataset" },
    el("div", { class: "dataset-head" },
      el("h3", {}, DATASET.title),
      el("span", { class: "price" }, fmt(DATASET.price), el("small", {}, "MATCH"))),
    el("p", {}, `Every Extraliga game the oracle has settled or cancelled: teams, final score, winner, the data provider's fixture id and the oracle's on-chain transaction. ${rows.length} games so far, growing after every matchday.`),
    el("p", { class: "split" }, `Paying burns ${fmt(burned)} MATCH (${Number(burnBps) / 100} %) for good and sends ${fmt(DATASET.price - burned)} MATCH to the community treasury.`),
    el("p", { class: "label" }, "Free sample: the three latest rows"),
    preview,
    action,
    el("p", { class: "fineprint" }, "Test network demo: the purchase shows the payment and the burn. The underlying audit log is also public on GitHub, so this is not a paywall.")));
}

function renderWallet() {
  const w = $("#wallet");
  if (!state.account) {
    if (!eth && isMobile()) {
      // Mobile browsers have no wallet extension: hand over to the MetaMask app's browser
      w.replaceChildren(el("a", { class: "btn", href: metamaskDeepLink() }, "Open in MetaMask app"));
      return;
    }
    w.replaceChildren(el("button", { class: "btn", type: "button", onclick: connect, disabled: state.connecting },
      state.connecting ? "Waiting for MetaMask…" : "Connect MetaMask"));
    return;
  }
  if (!state.chainOk) {
    w.replaceChildren(el("span", { class: "hint" }, "Your wallet is on another network."),
      el("button", { class: "btn", onclick: switchChain }, "Switch to Sepolia"));
    return;
  }
  if (state.isOracle) {
    // The account that settles games must never hold a stake in them
    w.replaceChildren(
      el("span", { class: "hint warn" }, "This is the oracle account. It settles games, so it must not bet. Switch to another account in MetaMask."),
      el("a", { class: "addr", href: `${CONFIG.explorer}/address/${state.account}`, target: "_blank", rel: "noopener" }, short(state.account)));
    return;
  }
  const now = Date.now() / 1000;
  const dripBtn = state.nextDrip > now
    ? el("span", { class: "hint" }, `Next free tokens in ${until(state.nextDrip)}`)
    : el("button", { class: "btn ghost", onclick: drip, disabled: state.busy || state.eth === 0n,
        title: state.eth === 0n ? "You need a little Sepolia ETH for gas first" : null }, "Get 100 free MATCH");
  w.replaceChildren(
    el("span", { class: "bal" }, fmt(state.balance), el("small", {}, "MATCH")),
    dripBtn,
    el("button", { class: "btn ghost", onclick: addTokenToWallet, title: "Show MATCH in MetaMask" }, "Add to MetaMask"),
    el("a", { class: "addr", href: `${CONFIG.explorer}/address/${state.account}`, target: "_blank", rel: "noopener" }, short(state.account)),
  );
  if (state.eth === 0n) {
    w.append(el("span", { class: "hint" }, "No Sepolia ETH for gas: ",
      el("a", { href: "https://cloud.google.com/application/web3/faucet/ethereum/sepolia", target: "_blank", rel: "noopener" }, "get some")));
  }
}

function crest(url, name) {
  const initials = name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2);
  if (!url) return el("span", { class: "crest mono", "aria-hidden": "true" }, initials);
  const img = el("img", { class: "crest", src: url, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
  img.addEventListener("error", () => img.replaceWith(el("span", { class: "crest mono", "aria-hidden": "true" }, initials)));
  return img;
}

function zone(m, c, side, { interactive, outcome }) {
  const pools = [c.homePool, c.awayPool];
  const total = pools[0] + pools[1];
  const share = total > 0n ? Number((pools[side] * 1000n) / total) / 10 : 0;
  const team = side === HOME ? m.home : m.away;
  const mine = state.mine.get(m.match_id);
  const myStake = mine ? (side === HOME ? mine.home : mine.away) : 0n;
  const mult = pools[side] > 0n ? Number(((total - (total * state.feeBps) / 10000n) * 100n) / pools[side]) / 100 : null;

  const kids = [
    el("span", { class: "fill", style: `--share:${share}%`, "aria-hidden": "true" }),
    crest(side === HOME ? m.home_logo : m.away_logo, team),
    el("span", { class: "side" }, side === HOME ? "Home" : "Away"),
    el("span", { class: "team" }, team),
    pools[side] > 0n
      ? el("span", { class: "pool" }, el("b", {}, fmt(pools[side])), " MATCH backed", ` (${share} %)`)
      : el("span", { class: "pool" }, c.status === "OPEN" ? "No stakes yet" : "No stakes"),
    mult && c.status === "OPEN"
      ? el("span", { class: "mult" }, pools[1 - side] === 0n
          ? `Only side backed: ×${mult.toFixed(2)} if they win, full refund if they lose`
          : `Pays ×${mult.toFixed(2)} right now`)
      : null,
    myStake > 0n ? el("span", { class: "mine" }, `Your stake: ${fmt(myStake)} MATCH`) : null,
    outcome === "won" ? el("span", { class: "flag" }, "Won") : null,
  ];
  const cls = `zone ${side === HOME ? "home" : "away"}${outcome === "lost" ? " lost" : ""}`;
  if (!interactive) return el("div", { class: cls }, kids);
  const picked = state.pick.get(m.match_id) === side;
  return el("button", {
    class: cls, type: "button", "aria-pressed": picked ? "true" : "false",
    "aria-label": `Bet on ${team}`,
    onclick: () => { state.pick.set(m.match_id, side); render(); queueMicrotask(() => document.getElementById(`amt-${m.match_id}`)?.focus()); },
  }, kids);
}

function circle(m, c, open) {
  const scored = m.score_home != null && m.score_away != null;
  if (c.status === "SETTLED" || (scored && !open)) {
    const extra = { AOT: "after overtime", AP: "after shootout" }[m.status_code] || (c.status === "SETTLED" ? "final" : "in play");
    return el("div", { class: "circle" }, el("span", { class: "big" }, `${m.score_home ?? "–"} : ${m.score_away ?? "–"}`), el("span", { class: "small" }, extra));
  }
  if (c.status === "CANCELLED") return el("div", { class: "circle" }, el("span", { class: "big" }, "Void"), el("span", { class: "small" }, "stakes refunded"));
  const left = until(c.start);
  if (left) return el("div", { class: "circle" }, el("span", { class: "big" }, left), el("span", { class: "small" }, "to face-off"));
  return el("div", { class: "circle" }, el("span", { class: "big" }, "Live"), el("span", { class: "small" }, "bets closed,", el("br"), "awaiting result"));
}

function ticket(m, c) {
  const side = state.pick.get(m.match_id);
  if (side == null || c.status !== "OPEN" || !until(c.start)) return null;
  const team = side === HOME ? m.home : m.away;
  const id = `amt-${m.match_id}`;

  const estimate = el("p", { class: "estimate" });
  const input = el("input", { id, type: "number", min: "1", step: "1", inputmode: "decimal",
    value: state.amount.get(m.match_id) ?? "25", "aria-describedby": `est-${m.match_id}` });
  estimate.id = `est-${m.match_id}`;
  const go = el("button", { class: `btn ${side === HOME ? "home" : "away"}`, type: "button" });

  const update = () => {
    state.amount.set(m.match_id, input.value);
    let amt = 0n;
    try { amt = ethers.parseUnits(input.value || "0", 18); } catch { amt = 0n; }
    const gain = payout(amt, side, c);
    const tooLow = amt < ONE, tooHigh = state.account && amt > state.balance;
    estimate.replaceChildren(
      tooHigh ? el("span", { class: "warn" }, `That is more than your ${fmt(state.balance)} MATCH. `) : "",
      tooLow ? el("span", { class: "warn" }, "The minimum stake is 1 MATCH. ") : "",
      "If ", el("b", {}, team), " win and the pool stays as it is, you get ",
      el("b", {}, `${fmt(gain)} MATCH`), ` back for ${fmt(amt)} staked. The odds move as other people bet.`);
    const needsAllowance = state.account && !state.isOracle && amt > state.allowance;
    if (needsAllowance) estimate.append(el("span", { class: "first" },
      " First bet from this wallet: MetaMask asks twice, once to let the pool use your MATCH and once for the bet. Later bets need one confirmation."));
    go.textContent = `Bet ${fmt(amt)} MATCH on ${team}`;
    go.disabled = state.busy || !state.account || !state.chainOk || tooLow || tooHigh || state.isOracle;
    go.title = state.isOracle ? "The oracle account cannot bet" : !state.account ? "Connect MetaMask first" : tooHigh ? "More than your MATCH balance" : tooLow ? "Minimum 1 MATCH" : "";
  };
  input.addEventListener("input", update);
  go.addEventListener("click", () => {
    let amt; try { amt = ethers.parseUnits(input.value, 18); } catch { return; }
    bet(m.match_id, side, amt);
  });
  const chips = [10, 25, 50, 100].map((v) => el("button", { class: "chip", type: "button", onclick: () => { input.value = v; update(); } }, String(v)));
  update();

  return el("div", { class: "ticket" },
    el("label", { for: id }, "Stake"),
    el("div", { class: "amount" }, input, ...chips),
    state.account ? go : el("button", { class: "btn", type: "button", onclick: connect }, "Connect MetaMask to bet"),
    estimate);
}

function claimBar(m, c) {
  const mine = state.mine.get(m.match_id);
  if (!mine || mine.claimable === 0n) return null;
  const what = c.status === "CANCELLED" ? "Your refund" : "Your winnings";
  return el("div", { class: "claim" },
    el("span", {}, `${what}: `, el("b", {}, `${fmt(mine.claimable)} MATCH`)),
    el("button", { class: "btn", type: "button", disabled: state.busy || !state.chainOk, onclick: () => claim(m.match_id) }, `Collect ${fmt(mine.claimable)} MATCH`));
}

function game(m) {
  const c = state.chain.get(m.match_id);
  if (!c || c.status === "NONE") return null;
  const open = c.status === "OPEN" && !!until(c.start);
  const outcome = (side) => c.status === "SETTLED" ? (c.winner === side ? "won" : "lost") : null;

  const auditUrl = `${CONFIG.repo}/blob/main/data/oracle-log.jsonl`;
  const meta = el("div", { class: "meta" },
    el("span", {}, open ? `Betting closes ${when(c.start)} Prague time` : `Face-off ${when(c.start)}`),
    open && state.pick.get(m.match_id) == null ? el("span", {}, "Tap a team to bet") : null,
    c.status !== "OPEN" ? el("a", { href: auditUrl, target: "_blank", rel: "noopener" }, "How the oracle decided") : null,
  );

  return el("article", { class: "game", "aria-label": `${m.home} vs ${m.away}` },
    el("div", { class: "rink" },
      zone(m, c, HOME, { interactive: open, outcome: outcome(HOME) }),
      zone(m, c, AWAY, { interactive: open, outcome: outcome(AWAY) }),
      circle(m, c, open)),
    meta,
    ticket(m, c),
    claimBar(m, c));
}

function render() {
  // Keep keyboard focus on the stake field across re-renders
  const focusedId = document.activeElement?.id;
  renderWallet();
  renderShop();
  const groups = { open: [], closed: [], done: [] };
  for (const m of [...state.board].sort((a, b) => a.start_ts - b.start_ts)) {
    const c = state.chain.get(m.match_id);
    if (!c || c.status === "NONE") continue;
    const g = c.status === "OPEN" ? (until(c.start) ? "open" : "closed") : "done";
    groups[g].push(m);
  }
  groups.done.reverse(); // newest results first

  const fill = (id, list, emptyText) => {
    const box = document.getElementById(id);
    const nodes = list.map(game).filter(Boolean);
    box.replaceChildren(...(nodes.length ? nodes : emptyText ? [el("p", { class: "empty" }, emptyText)] : []));
    return nodes.length;
  };
  fill("open", groups.open, state.board.length || state.chain.size
    ? "No games open right now. New fixtures appear here up to a week before face-off."
    : "Loading fixtures…");
  $("#closed-section").hidden = !fill("closed", groups.closed);
  $("#done-section").hidden = !fill("done", groups.done);
  if (focusedId?.startsWith("amt-")) {
    const again = document.getElementById(focusedId);
    if (again) { again.focus(); const v = again.value; again.value = ""; again.value = v; }
  }
}

// ------------------------------------------------------------------ boot
function staticLinks() {
  $("#repo-link").href = CONFIG.repo;
  $("#audit-link").href = `${CONFIG.repo}/blob/main/data/oracle-log.jsonl`;
  const link = (label, addr) => el("a", { href: `${CONFIG.explorer}/address/${addr}`, target: "_blank", rel: "noopener" }, label);
  $("#contracts").replaceChildren("Contracts on Sepolia: ", link("MATCH token", CONFIG.token), ", ",
    link("betting pool", CONFIG.pool), ", ", link("faucet", CONFIG.faucet), ".");
}

async function boot() {
  staticLinks();
  // Wallets answer the EIP-6963 request asynchronously; give them a moment
  await new Promise((r) => setTimeout(r, 300));
  eth = pickWallet();
  console.info("[EPM] wallets announced:", [...announced.keys()].join(", ") || "none",
    "| window.ethereum:", window.ethereum ? (window.ethereum.isMetaMask ? "MetaMask" : "other") : "none");
  if (eth) {
    // Reconnect silently if the site is already authorised, without a popup
    try {
      const accounts = await eth.request({ method: "eth_accounts" });
      if (accounts.length) { state.account = ethers.getAddress(accounts[0]); await checkChain(); }
    } catch (e) { console.warn("[EPM] silent reconnect failed", e); }
    eth.on?.("accountsChanged", async (a) => {
      state.account = a[0] ? ethers.getAddress(a[0]) : null;
      try { await Promise.all([loadMine(), loadOwned()]); } catch { /* next refresh */ }
      render();
    });
    eth.on?.("chainChanged", async () => { await checkChain(); try { await loadMine(); } catch { /* next refresh */ } render(); });
  }
  render();
  await refresh();
  setInterval(refresh, 60_000);                       // pools and results
  setInterval(() => { if (!state.busy && !document.activeElement?.closest?.(".ticket")) render(); }, 30_000); // countdowns
}

boot();
