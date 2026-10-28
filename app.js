// Stock Alert Pro - Multi-User Client Application

let stocksData = [];
let activeFilter = 'all';
let ws = null;
let reconnectTimer = null;
let searchDebounceTimer = null;
let authToken = localStorage.getItem("stock_auth_token") || null;
let currentUser = null;

let userSettings = {
  refresh_interval_sec: 15,
  default_threshold_pct: 1.0,
  sound_enabled: true,
  browser_notifications: true,
  desktop_toast: true
};

// --- Auth Fetch Helper ---
async function authFetch(url, options = {}) {
  options.headers = options.headers || {};
  if (authToken) {
    options.headers["Authorization"] = `Bearer ${authToken}`;
  }
  const res = await fetch(url, options);
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("Unauthorized");
  }
  return res;
}

function handleUnauthorized() {
  localStorage.removeItem("stock_auth_token");
  authToken = null;
  currentUser = null;
  if (ws) {
    ws.close();
    ws = null;
  }
  showAuthModal();
}

// --- Auth Modal & UI Management ---
function showAuthModal() {
  const modal = document.getElementById("authModal");
  if (modal) modal.classList.remove("hidden");
  const userArea = document.getElementById("userProfileArea");
  if (userArea) userArea.classList.add("hidden");
}

function hideAuthModal() {
  const modal = document.getElementById("authModal");
  if (modal) modal.classList.add("hidden");
}

function switchAuthTab(tab) {
  const tabLogin = document.getElementById("tabAuthLogin");
  const tabReg = document.getElementById("tabAuthRegister");
  const formLogin = document.getElementById("formLogin");
  const formReg = document.getElementById("formRegister");
  const errBox = document.getElementById("authErrorMsg");
  errBox.classList.add("hidden");

  if (tab === "login") {
    tabLogin.classList.add("bg-emerald-500/20", "text-emerald-400", "border-emerald-500/30", "font-semibold");
    tabLogin.classList.remove("text-slate-400");
    tabReg.classList.remove("bg-emerald-500/20", "text-emerald-400", "border-emerald-500/30", "font-semibold");
    tabReg.classList.add("text-slate-400");
    formLogin.classList.remove("hidden");
    formReg.classList.add("hidden");
  } else {
    tabReg.classList.add("bg-emerald-500/20", "text-emerald-400", "border-emerald-500/30", "font-semibold");
    tabReg.classList.remove("text-slate-400");
    tabLogin.classList.remove("bg-emerald-500/20", "text-emerald-400", "border-emerald-500/30", "font-semibold");
    tabLogin.classList.add("text-slate-400");
    formReg.classList.remove("hidden");
    formLogin.classList.add("hidden");
  }
}

async function submitLogin(e) {
  e.preventDefault();
  const username = document.getElementById("loginUsername").value.trim();
  const password = document.getElementById("loginPassword").value;
  const errBox = document.getElementById("authErrorMsg");
  const btn = document.getElementById("btnLoginSubmit");

  errBox.classList.add("hidden");
  btn.disabled = true;
  btn.innerText = "กำลังเข้าสู่ระบบ...";

  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "เข้าสู่ระบบไม่สำเร็จ");
    }

    authToken = data.token;
    currentUser = data.user;
    localStorage.setItem("stock_auth_token", authToken);

    hideAuthModal();
    initUserDashboard();
  } catch (err) {
    errBox.innerText = err.message;
    errBox.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.innerText = "เข้าสู่ระบบ";
  }
}

async function submitRegister(e) {
  e.preventDefault();
  const username = document.getElementById("regUsername").value.trim();
  const displayName = document.getElementById("regDisplayName").value.trim();
  const password = document.getElementById("regPassword").value;
  const errBox = document.getElementById("authErrorMsg");
  const btn = document.getElementById("btnRegSubmit");

  errBox.classList.add("hidden");
  btn.disabled = true;
  btn.innerText = "กำลังสร้างบัญชี...";

  try {
    const res = await fetch("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, display_name: displayName })
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "สมัครสมาชิกไม่สำเร็จ");
    }

    authToken = data.token;
    currentUser = data.user;
    localStorage.setItem("stock_auth_token", authToken);

    hideAuthModal();
    initUserDashboard();
  } catch (err) {
    errBox.innerText = err.message;
    errBox.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.innerText = "สร้างบัญชีและเริ่มใช้งาน";
  }
}

async function handleLogout() {
  if (authToken) {
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        headers: { "Authorization": `Bearer ${authToken}` }
      });
    } catch (e) {}
  }
  handleUnauthorized();
}

// --- Check Auth on Startup ---
async function checkAuthAndInit() {
  if (!authToken) {
    showAuthModal();
    return;
  }

  try {
    const res = await fetch("/api/auth/me", {
      headers: { "Authorization": `Bearer ${authToken}` }
    });
    if (!res.ok) {
      throw new Error("Session expired");
    }
    currentUser = await res.json();
    hideAuthModal();
    initUserDashboard();
  } catch (e) {
    handleUnauthorized();
  }
}

function initUserDashboard() {
  // Update user header profile
  const userArea = document.getElementById("userProfileArea");
  const userName = document.getElementById("navUserName");
  if (userArea && currentUser) {
    userName.innerText = currentUser.display_name || currentUser.username;
    userArea.classList.remove("hidden");
    userArea.classList.add("flex");
  }

  connectWebSocket();
  loadSettings();
  loadAlertHistory();
  refreshNow();
}

// --- Web Audio Synthesizer (Chime) ---
let audioCtx = null;
function getAudioContext() {
  if (!audioCtx) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass) {
      audioCtx = new AudioContextClass();
    }
  }
  if (audioCtx && audioCtx.state === 'suspended') {
    audioCtx.resume();
  }
  return audioCtx;
}

function playAlertSound() {
  if (!userSettings.sound_enabled) return;
  try {
    const ctx = getAudioContext();
    if (!ctx) return;

    const now = ctx.currentTime;
    const osc1 = ctx.createOscillator();
    const gain1 = ctx.createGain();
    osc1.type = 'sine';
    osc1.frequency.setValueAtTime(659.25, now);
    gain1.gain.setValueAtTime(0.25, now);
    gain1.gain.exponentialRampToValueAtTime(0.001, now + 0.4);
    osc1.connect(gain1);
    gain1.connect(ctx.destination);
    osc1.start(now);
    osc1.stop(now + 0.4);

    const osc2 = ctx.createOscillator();
    const gain2 = ctx.createGain();
    osc2.type = 'sine';
    osc2.frequency.setValueAtTime(987.77, now + 0.15);
    gain2.gain.setValueAtTime(0.3, now + 0.15);
    gain2.gain.exponentialRampToValueAtTime(0.001, now + 0.7);
    osc2.connect(gain2);
    gain2.connect(ctx.destination);
    osc2.start(now + 0.15);
    osc2.stop(now + 0.7);
  } catch (err) {
    console.warn("Audio playback error:", err);
  }
}

// --- Browser Notification API ---
function updateNotificationButton() {
  const btn = document.getElementById("btnEnableNotifications");
  const text = document.getElementById("notifStatusText");
  if (!("Notification" in window)) {
    if (btn) btn.style.display = "none";
    return;
  }
  if (Notification.permission === "granted") {
    if (text) text.innerText = "เปิดแจ้งเตือนแล้ว";
    if (btn) btn.classList.add("text-emerald-400", "border-emerald-500/30");
  } else if (Notification.permission === "denied") {
    if (text) text.innerText = "แจ้งเตือนถูกปิดไว้";
    if (btn) btn.classList.add("text-rose-400");
  } else {
    if (text) text.innerText = "เปิดแจ้งเตือน";
  }
}

async function requestNotificationPermission() {
  if (!("Notification" in window)) {
    alert("เบราว์เซอร์นี้ไม่รองรับ HTML5 Desktop Notification");
    return;
  }
  const perm = await Notification.requestPermission();
  updateNotificationButton();
  if (perm === "granted") {
    new Notification("Stock Alert Pro", {
      body: "ระบบเปิดการแจ้งเตือนสำเร็จ เมื่อหุ้นเข้าใกล้ 1% จะแจ้งเตือนทันที",
      icon: "https://cdn-icons-png.flaticon.com/512/2991/2991148.png"
    });
    getAudioContext();
  }
}

function showBrowserNotification(title, message) {
  if (!userSettings.browser_notifications) return;
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      new Notification(title, {
        body: message,
        icon: "https://cdn-icons-png.flaticon.com/512/2991/2991148.png",
        tag: "stock-alert-" + Date.now(),
        renotify: true
      });
    } catch (e) {
      console.warn("Notification error:", e);
    }
  }
}

// --- Sound Toggle ---
function toggleSound() {
  userSettings.sound_enabled = !userSettings.sound_enabled;
  updateSoundIcon();
  if (userSettings.sound_enabled) {
    playAlertSound();
  }
  authFetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sound_enabled: userSettings.sound_enabled })
  });
}

function updateSoundIcon() {
  const icon = document.getElementById("soundIcon");
  if (icon) {
    if (userSettings.sound_enabled) {
      icon.setAttribute("data-lucide", "volume-2");
      icon.classList.remove("text-slate-500");
      icon.classList.add("text-emerald-400");
    } else {
      icon.setAttribute("data-lucide", "volume-x");
      icon.classList.remove("text-emerald-400");
      icon.classList.add("text-slate-500");
    }
    lucide.createIcons();
  }
}

// --- WebSocket Connection ---
function connectWebSocket() {
  if (!authToken) return;
  if (ws) {
    ws.close();
    ws = null;
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws?token=${encodeURIComponent(authToken)}`;

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log("WebSocket connected for user:", currentUser?.username);
    if (reconnectTimer) {
      clearInterval(reconnectTimer);
      reconnectTimer = null;
    }
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "INIT" || msg.type === "STOCKS_UPDATE") {
        stocksData = msg.data || [];
        renderDashboard();
        
        if (msg.timestamp) {
          const timeElem = document.getElementById("lastUpdatedTime");
          if (timeElem) timeElem.innerText = msg.timestamp;
        }

        // Handle alerts
        if (msg.alerts && msg.alerts.length > 0) {
          msg.alerts.forEach(alert => {
            showBrowserNotification(alert.title, alert.message);
          });
          if (msg.sound) {
            playAlertSound();
          }
          showActiveBanner(msg.alerts);
          loadAlertHistory();
        }
      }
    } catch (err) {
      console.error("WS message parse error:", err);
    }
  };

  ws.onclose = (e) => {
    if (e.code === 1008) {
      handleUnauthorized();
      return;
    }
    if (authToken && !reconnectTimer) {
      reconnectTimer = setTimeout(connectWebSocket, 3000);
    }
  };

  ws.onerror = (err) => {
    ws?.close();
  };
}

// --- Dashboard Rendering ---
function renderDashboard() {
  updateKPIs();
  renderStocksTable();
  lucide.createIcons();
}

function updateKPIs() {
  const total = stocksData.length;
  let approachingCount = 0;
  let reachedCount = 0;
  let normalCount = 0;
  let closestItem = null;
  let minDiff = Infinity;

  stocksData.forEach(item => {
    const prox = item.proximity || {};
    if (prox.status === "APPROACHING") {
      approachingCount++;
    } else if (prox.status === "REACHED") {
      reachedCount++;
    } else {
      normalCount++;
    }

    if (prox.abs_diff_percent !== null && prox.abs_diff_percent !== undefined) {
      if (prox.abs_diff_percent < minDiff) {
        minDiff = prox.abs_diff_percent;
        closestItem = item;
      }
    }
  });

  document.getElementById("statTotal").innerText = total;
  document.getElementById("statApproaching").innerText = approachingCount;
  document.getElementById("statReached").innerText = reachedCount;

  const statClosest = document.getElementById("statClosest");
  const statClosestDiff = document.getElementById("statClosestDiff");
  if (closestItem && minDiff !== Infinity) {
    statClosest.innerText = closestItem.symbol;
    statClosestDiff.innerText = `ห่างเพียง ${minDiff.toFixed(2)}% (เป้า ${closestItem.target_price.toLocaleString()})`;
  } else {
    statClosest.innerText = "-";
    statClosestDiff.innerText = "ห่าง: -";
  }

  document.getElementById("countAll").innerText = total;
  document.getElementById("countApproaching").innerText = approachingCount;
  document.getElementById("countReached").innerText = reachedCount;
  document.getElementById("countNormal").innerText = normalCount;

  const cardApproaching = document.getElementById("cardApproachingAlerts");
  if (approachingCount > 0) {
    cardApproaching.classList.add("border-amber-500/80", "shadow-amber-500/20", "shadow-lg");
  } else {
    cardApproaching.classList.remove("border-amber-500/80", "shadow-amber-500/20", "shadow-lg");
  }
}

function renderStocksTable() {
  const tbody = document.getElementById("stocksTableBody");
  const emptyState = document.getElementById("emptyState");
  const searchInput = (document.getElementById("filterInput").value || "").trim().toLowerCase();
  const sortBy = document.getElementById("sortBy").value;

  let filtered = stocksData.filter(item => {
    const prox = item.proximity || {};
    if (activeFilter === "approaching" && prox.status !== "APPROACHING") return false;
    if (activeFilter === "reached" && prox.status !== "REACHED") return false;
    if (activeFilter === "normal" && (prox.status === "APPROACHING" || prox.status === "REACHED")) return false;

    if (searchInput) {
      const matchSym = item.symbol.toLowerCase().includes(searchInput);
      const matchName = (item.name || "").toLowerCase().includes(searchInput);
      const matchNotes = (item.notes || "").toLowerCase().includes(searchInput);
      if (!matchSym && !matchName && !matchNotes) return false;
    }
    return true;
  });

  filtered.sort((a, b) => {
    const aProx = a.proximity?.abs_diff_percent ?? 999;
    const bProx = b.proximity?.abs_diff_percent ?? 999;
    if (sortBy === "proximity") return aProx - bProx;
    if (sortBy === "change") return (b.quote?.change_percent ?? 0) - (a.quote?.change_percent ?? 0);
    if (sortBy === "symbol") return a.symbol.localeCompare(b.symbol);
    return 0;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = "";
    emptyState.classList.remove("hidden");
    return;
  }

  emptyState.classList.add("hidden");

  let html = "";
  filtered.forEach(item => {
    const quote = item.quote || {};
    const prox = item.proximity || {};
    const price = quote.price !== undefined ? quote.price : null;
    const change = quote.change || 0;
    const changePct = quote.change_percent || 0;
    const currency = quote.currency || (item.symbol.endsWith(".BK") ? "THB" : "USD");
    const isThai = item.symbol.endsWith(".BK") || quote.exchange === "SET";

    const isPositive = change >= 0;
    const changeColorClass = isPositive ? "text-emerald-400" : "text-rose-400";
    const changeBgClass = isPositive ? "bg-emerald-500/10" : "bg-rose-500/10";
    const changeSign = isPositive ? "+" : "";

    const isApproaching = prox.status === "APPROACHING";
    const isReached = prox.status === "REACHED";

    let rowClasses = "hover:bg-slate-800/40 transition group";
    if (isApproaching) {
      rowClasses += " bg-amber-500/[0.04]";
    } else if (isReached) {
      rowClasses += " bg-emerald-500/[0.04]";
    }

    let statusBadge = "";
    const threshold = item.alert_threshold_pct || userSettings.default_threshold_pct;
    const absDiff = prox.abs_diff_percent !== undefined && prox.abs_diff_percent !== null ? prox.abs_diff_percent : null;
    
    if (isReached) {
      statusBadge = `
        <span class="inline-flex items-center px-2.5 py-1 rounded-full text-[11px] font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/40 shadow-sm">
          <i data-lucide="check-circle" class="w-3.5 h-3.5 mr-1"></i>
          ถึงเป้าหมายแล้ว
        </span>
      `;
    } else if (isApproaching) {
      statusBadge = `
        <span class="approaching-badge inline-flex items-center px-2.5 py-1 rounded-full text-[11px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/50 shadow-md shadow-amber-500/20 animate-pulse">
          <span class="w-2 h-2 rounded-full bg-amber-400 mr-1.5 animate-ping"></span>
          🚨 ใกล้เป้า (ห่าง ${absDiff.toFixed(2)}%)
        </span>
      `;
    } else if (absDiff !== null) {
      statusBadge = `
        <span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-slate-800 text-slate-400 border border-slate-700">
          กำลังติดตาม (ห่าง ${absDiff.toFixed(2)}%)
        </span>
      `;
    } else {
      statusBadge = `<span class="text-slate-500 text-[11px]">รอข้อมูลราคา...</span>`;
    }

    let progressPercent = 0;
    if (absDiff !== null) {
      progressPercent = Math.max(0, Math.min(100, Math.round(100 - (absDiff / 5) * 100)));
    }
    const barColor = isReached ? 'bg-emerald-400' : isApproaching ? 'bg-amber-400' : 'bg-slate-600';

    html += `
      <tr class="${rowClasses}" id="row-${item.id}">
        <td class="py-3 px-4">
          <div class="flex items-center space-x-2.5">
            <div class="w-8 h-8 rounded-lg ${isThai ? 'bg-blue-500/10 text-blue-400 border border-blue-500/20' : 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20'} flex items-center justify-center font-bold text-xs shrink-0">
              ${isThai ? 'SET' : 'US'}
            </div>
            <div class="min-w-0">
              <div class="flex items-center space-x-1.5">
                <span class="font-bold text-white text-sm hover:text-emerald-400 cursor-pointer" onclick="openEditStockModal('${item.id}')">${item.symbol}</span>
                ${item.notes ? `<span class="text-[10px] px-1.5 py-0.2 rounded bg-slate-800 text-slate-400 border border-slate-700 truncate max-w-[120px]" title="${item.notes}">${item.notes}</span>` : ''}
              </div>
              <p class="text-[11px] text-slate-400 truncate max-w-[180px]">${item.name || item.symbol}</p>
            </div>
          </div>
        </td>

        <td class="py-3 px-4 text-right">
          <span class="text-sm sm:text-base font-bold font-mono text-white" id="price-${item.id}">
            ${price !== null ? price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 }) : '--'}
          </span>
          <span class="text-[10px] text-slate-400 ml-1 font-mono">${currency}</span>
        </td>

        <td class="py-3 px-4 text-right">
          <div class="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold font-mono ${changeBgClass} ${changeColorClass}">
            <i data-lucide="${isPositive ? 'arrow-up-right' : 'arrow-down-right'}" class="w-3.5 h-3.5 mr-0.5"></i>
            ${changeSign}${changePct.toFixed(2)}%
          </div>
        </td>

        <td class="py-3 px-4 text-right">
          <span class="text-sm font-semibold font-mono text-cyan-300">
            ${item.target_price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}
          </span>
          <span class="text-[10px] text-slate-400 ml-0.5 font-mono">${currency}</span>
          <p class="text-[10px] text-slate-500">เกณฑ์เตือน ≤ ${threshold}%</p>
        </td>

        <td class="py-3 px-4 text-center">
          <div class="w-32 mx-auto space-y-1">
            <div class="flex items-center justify-between text-[10px] font-mono">
              <span class="text-slate-400">ห่าง</span>
              <span class="${isApproaching ? 'text-amber-400 font-bold' : isReached ? 'text-emerald-400 font-bold' : 'text-slate-300'}">
                ${absDiff !== null ? `${absDiff.toFixed(2)}%` : '--'}
              </span>
            </div>
            <div class="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
              <div class="${barColor} h-1.5 rounded-full transition-all duration-500" style="width: ${progressPercent}%"></div>
            </div>
          </div>
        </td>

        <td class="py-3 px-4 text-center">
          ${statusBadge}
        </td>

        <td class="py-3 px-4 text-center">
          <canvas id="spark-${item.id}" class="sparkline-canvas mx-auto" width="100" height="32"></canvas>
        </td>

        <td class="py-3 px-4 text-right">
          <div class="flex items-center justify-end space-x-1">
            ${isApproaching ? `
              <button onclick="acknowledgeAlert('${item.id}')" class="p-1.5 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/40 transition" title="รับทราบการเตือน (ปิดเสียงชั่วคราว)">
                <i data-lucide="check" class="w-3.5 h-3.5"></i>
              </button>
            ` : ''}
            <button onclick="openEditStockModal('${item.id}')" class="p-1.5 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-white transition" title="แก้ไขราคาเป้าหมาย">
              <i data-lucide="edit-3" class="w-3.5 h-3.5"></i>
            </button>
            <button onclick="deleteStock('${item.id}', '${item.symbol}')" class="p-1.5 rounded-lg hover:bg-rose-500/20 text-slate-400 hover:text-rose-400 transition" title="ลบออกจาก Shortlist">
              <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
            </button>
          </div>
        </td>
      </tr>
    `;
  });

  tbody.innerHTML = html;

  filtered.forEach(item => {
    const quote = item.quote;
    if (quote && quote.sparkline && quote.sparkline.length > 2) {
      drawSparkline(`spark-${item.id}`, quote.sparkline, quote.change >= 0);
    }
  });
}

function drawSparkline(canvasId, points, isPositive) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;

  ctx.clearRect(0, 0, w, h);
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;

  ctx.beginPath();
  points.forEach((val, i) => {
    const x = (i / (points.length - 1)) * (w - 6) + 3;
    const y = h - ((val - min) / range) * (h - 8) - 4;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });

  ctx.strokeStyle = isPositive ? '#10b981' : '#f43f5e';
  ctx.lineWidth = 1.75;
  ctx.stroke();
}

// --- Filter & Search Controls ---
function setFilter(filter) {
  activeFilter = filter;
  document.querySelectorAll('.filter-tab').forEach(btn => {
    btn.classList.remove('bg-emerald-500/20', 'text-emerald-300', 'border', 'border-emerald-500/30');
    btn.classList.add('text-slate-400');
  });
  const activeBtn = document.getElementById(`tab-${filter}`);
  if (activeBtn) {
    activeBtn.classList.remove('text-slate-400');
    activeBtn.classList.add('bg-emerald-500/20', 'text-emerald-300', 'border', 'border-emerald-500/30');
  }
  renderStocksTable();
  lucide.createIcons();
}

function applyTableFilter() {
  renderStocksTable();
  lucide.createIcons();
}

// --- Force Refresh ---
async function refreshNow() {
  const icon = document.getElementById("refreshIcon");
  if (icon) icon.classList.add("animate-spin");
  try {
    await authFetch("/api/refresh", { method: "POST" });
    const res = await authFetch("/api/stocks");
    stocksData = await res.json();
    renderDashboard();
  } catch (err) {
    console.error("Refresh error:", err);
  } finally {
    setTimeout(() => {
      if (icon) icon.classList.remove("animate-spin");
    }, 800);
  }
}

// --- Banner Alert ---
function showActiveBanner(alerts) {
  const banner = document.getElementById("activeAlertBanner");
  const title = document.getElementById("bannerTitle");
  const desc = document.getElementById("bannerDesc");
  if (!banner || !alerts || alerts.length === 0) return;

  const firstAlert = alerts[0];
  title.innerText = `${firstAlert.title}`;
  desc.innerText = `${firstAlert.message}`;
  banner.classList.remove("hidden");
}

function dismissBanner() {
  const banner = document.getElementById("activeAlertBanner");
  if (banner) banner.classList.add("hidden");
}

// --- Acknowledge Alert ---
async function acknowledgeAlert(stockId) {
  await authFetch(`/api/stocks/${stockId}/ack`, { method: "POST" });
  const row = document.getElementById(`row-${stockId}`);
  if (row) {
    row.classList.remove("bg-amber-500/[0.04]");
  }
}

// --- Add Stock Modal & Autocomplete ---
let selectedSearchResult = null;

function openAddStockModal() {
  getAudioContext();
  selectedSearchResult = null;
  document.getElementById("inputStockSearch").value = "";
  document.getElementById("inputTargetPrice").value = "";
  document.getElementById("inputNotes").value = "";
  document.getElementById("inputThreshold").value = userSettings.default_threshold_pct || 1.0;
  document.getElementById("stockPreviewCard").classList.add("hidden");
  document.getElementById("searchDropdown").classList.add("hidden");
  document.getElementById("priceQuickHints").innerHTML = "";
  document.getElementById("addStockModal").classList.remove("hidden");
  document.getElementById("inputStockSearch").focus();
}

function closeAddStockModal() {
  document.getElementById("addStockModal").classList.add("hidden");
  document.getElementById("searchDropdown").classList.add("hidden");
}

function handleStockSearch(val) {
  clearTimeout(searchDebounceTimer);
  const q = val.trim();
  const dropdown = document.getElementById("searchDropdown");
  const spinner = document.getElementById("searchSpinner");

  if (!q) {
    dropdown.classList.add("hidden");
    return;
  }

  spinner.classList.remove("hidden");
  searchDebounceTimer = setTimeout(async () => {
    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      const results = await res.json();
      spinner.classList.add("hidden");
      renderSearchResults(results);
    } catch (err) {
      spinner.classList.add("hidden");
      console.error("Search error:", err);
    }
  }, 250);
}

function renderSearchResults(results) {
  const dropdown = document.getElementById("searchDropdown");
  if (!results || results.length === 0) {
    dropdown.innerHTML = `<div class="p-3 text-xs text-slate-500 text-center">ไม่พบผลการค้นหา กดบันทึกเพื่อใช้สัญลักษณ์นี้ได้เลย</div>`;
    dropdown.classList.remove("hidden");
    return;
  }

  let html = "";
  results.forEach(item => {
    const isThai = item.is_thai;
    html += `
      <div onclick='selectSearchStock(${JSON.stringify(item)})' class="p-2.5 hover:bg-slate-800 cursor-pointer flex items-center justify-between transition">
        <div>
          <div class="flex items-center space-x-1.5">
            <span class="font-bold text-white text-xs">${item.symbol}</span>
            <span class="text-[10px] px-1.5 py-0.2 rounded ${isThai ? 'bg-blue-500/20 text-blue-400' : 'bg-slate-700 text-slate-300'} font-semibold">${item.exchange || 'STOCK'}</span>
          </div>
          <p class="text-[11px] text-slate-400 truncate max-w-[220px]">${item.name}</p>
        </div>
        <i data-lucide="chevron-right" class="w-4 h-4 text-slate-500"></i>
      </div>
    `;
  });

  dropdown.innerHTML = html;
  dropdown.classList.remove("hidden");
  lucide.createIcons();
}

async function selectSearchStock(item) {
  selectedSearchResult = item;
  document.getElementById("inputStockSearch").value = item.symbol;
  document.getElementById("searchDropdown").classList.add("hidden");

  const preview = document.getElementById("stockPreviewCard");
  document.getElementById("previewSymbol").innerText = item.symbol;
  document.getElementById("previewExchange").innerText = item.exchange;
  document.getElementById("previewName").innerText = item.name;
  document.getElementById("previewPrice").innerText = "กำลังดึงราคา...";
  preview.classList.remove("hidden");

  try {
    const existing = stocksData.find(s => s.symbol === item.symbol);
    if (existing && existing.quote) {
      updatePreviewWithPrice(existing.quote.price, existing.quote.currency);
    } else {
      document.getElementById("previewPrice").innerText = "พร้อมบันทึก";
    }
  } catch (e) {
    document.getElementById("previewPrice").innerText = "--";
  }

  document.getElementById("inputTargetPrice").focus();
}

function updatePreviewWithPrice(price, currency) {
  document.getElementById("previewPrice").innerText = `${price.toLocaleString()} ${currency}`;
  const hints = document.getElementById("priceQuickHints");
  const p1Up = (price * 1.01).toFixed(2);
  const p1Down = (price * 0.99).toFixed(2);
  hints.innerHTML = `
    <span onclick="setQuickTarget(${p1Down})" class="hover:underline">ย่อ 1% (${p1Down})</span> |
    <span onclick="setQuickTarget(${p1Up})" class="hover:underline">ขึ้น 1% (${p1Up})</span>
  `;
}

function setQuickTarget(val) {
  document.getElementById("inputTargetPrice").value = val;
}

async function submitAddStock() {
  const rawSymbol = document.getElementById("inputStockSearch").value.trim().toUpperCase();
  const targetPrice = parseFloat(document.getElementById("inputTargetPrice").value);
  const threshold = parseFloat(document.getElementById("inputThreshold").value) || 1.0;
  const direction = document.getElementById("inputDirection").value;
  const notes = document.getElementById("inputNotes").value.trim();

  if (!rawSymbol) {
    alert("กรุณาระบุสัญลักษณ์หุ้น (เช่น PTT หรือ NVDA)");
    return;
  }
  if (isNaN(targetPrice) || targetPrice <= 0) {
    alert("กรุณาระบุราคาเป้าหมายที่ถูกต้อง");
    return;
  }

  const btn = document.getElementById("btnSubmitStock");
  btn.disabled = true;
  btn.innerText = "กำลังบันทึก...";

  try {
    const payload = {
      symbol: rawSymbol,
      target_price: targetPrice,
      alert_threshold_pct: threshold,
      direction: direction,
      notes: notes,
      name: selectedSearchResult ? selectedSearchResult.name : rawSymbol
    };

    const res = await authFetch("/api/stocks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      throw new Error("บันทึกไม่สำเร็จ");
    }

    closeAddStockModal();
    refreshNow();
  } catch (err) {
    alert("เกิดข้อผิดพลาด: " + err.message);
  } finally {
    btn.disabled = false;
    btn.innerText = "บันทึกเข้า Shortlist";
  }
}

// --- Edit Stock Modal ---
function openEditStockModal(stockId) {
  const item = stocksData.find(s => s.id === stockId);
  if (!item) return;

  document.getElementById("editStockId").value = item.id;
  document.getElementById("editStockSymbol").innerText = item.symbol;
  document.getElementById("editStockName").innerText = item.name || item.symbol;
  document.getElementById("editTargetPrice").value = item.target_price;
  document.getElementById("editThreshold").value = item.alert_threshold_pct || 1.0;
  document.getElementById("editDirection").value = item.direction || "both";
  document.getElementById("editNotes").value = item.notes || "";

  document.getElementById("editStockModal").classList.remove("hidden");
}

function closeEditStockModal() {
  document.getElementById("editStockModal").classList.add("hidden");
}

async function submitEditStock() {
  const stockId = document.getElementById("editStockId").value;
  const targetPrice = parseFloat(document.getElementById("editTargetPrice").value);
  const threshold = parseFloat(document.getElementById("editThreshold").value) || 1.0;
  const direction = document.getElementById("editDirection").value;
  const notes = document.getElementById("editNotes").value.trim();

  if (isNaN(targetPrice) || targetPrice <= 0) {
    alert("กรุณาระบุราคาเป้าหมายที่ถูกต้อง");
    return;
  }

  try {
    const res = await authFetch(`/api/stocks/${stockId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_price: targetPrice,
        alert_threshold_pct: threshold,
        direction: direction,
        notes: notes
      })
    });

    if (res.ok) {
      closeEditStockModal();
      refreshNow();
    }
  } catch (err) {
    alert("แก้ไขไม่สำเร็จ: " + err.message);
  }
}

// --- Delete Stock ---
async function deleteStock(stockId, symbol) {
  if (!confirm(`คุณต้องการลบหุ้น ${symbol} ออกจาก Shortlist หรือไม่?`)) {
    return;
  }
  try {
    const res = await authFetch(`/api/stocks/${stockId}`, { method: "DELETE" });
    if (res.ok) {
      stocksData = stocksData.filter(s => s.id !== stockId);
      renderDashboard();
    }
  } catch (err) {
    alert("ลบไม่สำเร็จ: " + err.message);
  }
}

// --- Settings Modal ---
async function loadSettings() {
  try {
    const res = await authFetch("/api/settings");
    userSettings = await res.json();
    document.getElementById("settingInterval").value = userSettings.refresh_interval_sec || 15;
    document.getElementById("settingThreshold").value = userSettings.default_threshold_pct || 1.0;
    document.getElementById("settingSound").checked = !!userSettings.sound_enabled;
    document.getElementById("settingBrowserNotif").checked = !!userSettings.browser_notifications;
    document.getElementById("settingDesktopToast").checked = !!userSettings.desktop_toast;
    updateSoundIcon();
  } catch (err) {
    console.warn("Could not load settings:", err);
  }
}

function openSettingsModal() {
  loadSettings();
  document.getElementById("settingsModal").classList.remove("hidden");
}

function closeSettingsModal() {
  document.getElementById("settingsModal").classList.add("hidden");
}

async function saveSettings() {
  const updates = {
    refresh_interval_sec: parseInt(document.getElementById("settingInterval").value) || 15,
    default_threshold_pct: parseFloat(document.getElementById("settingThreshold").value) || 1.0,
    sound_enabled: document.getElementById("settingSound").checked,
    browser_notifications: document.getElementById("settingBrowserNotif").checked,
    desktop_toast: document.getElementById("settingDesktopToast").checked
  };

  try {
    const res = await authFetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates)
    });
    if (res.ok) {
      closeSettingsModal();
      refreshNow();
    }
  } catch (err) {
    alert("บันทึกการตั้งค่าไม่สำเร็จ: " + err.message);
  }
}

async function testAlertSystem() {
  playAlertSound();
  showBrowserNotification("🔔 ทดสอบการแจ้งเตือนหุ้น", "ระบบแจ้งเตือนทำงานได้สมบูรณ์แบบ!");
}

// --- Alerts History Drawer ---
function toggleAlertsDrawer() {
  const drawer = document.getElementById("alertsDrawer");
  if (drawer.classList.contains("hidden")) {
    drawer.classList.remove("hidden");
    loadAlertHistory();
  } else {
    drawer.classList.add("hidden");
  }
}

async function loadAlertHistory() {
  try {
    const res = await authFetch("/api/alerts");
    const alerts = await res.json();
    const container = document.getElementById("alertsList");
    const badge = document.getElementById("alertCountBadge");

    if (alerts && alerts.length > 0) {
      badge.innerText = alerts.length;
      badge.classList.remove("hidden");

      let html = "";
      alerts.forEach(a => {
        const isReached = a.status === "REACHED";
        html += `
          <div class="p-3 rounded-xl bg-slate-900 border border-slate-800 space-y-1">
            <div class="flex items-center justify-between">
              <span class="font-bold text-xs ${isReached ? 'text-emerald-400' : 'text-amber-400'}">${a.symbol}</span>
              <span class="text-[10px] text-slate-500 font-mono">${a.timestamp}</span>
            </div>
            <p class="text-xs text-white font-medium">${a.title || 'แจ้งเตือนราคา'}</p>
            <p class="text-[11px] text-slate-400 font-mono">${a.message || ''}</p>
          </div>
        `;
      });
      container.innerHTML = html;
    } else {
      badge.classList.add("hidden");
      container.innerHTML = `<p class="text-center text-xs text-slate-500 py-8">ไม่มีประวัติการแจ้งเตือน</p>`;
    }
  } catch (err) {
    console.error("Alerts history error:", err);
  }
}

async function clearAlertHistory() {
  if (confirm("คุณต้องการล้างประวัติการแจ้งเตือนทั้งหมดหรือไม่?")) {
    await authFetch("/api/alerts", { method: "DELETE" });
    loadAlertHistory();
  }
}

// --- Cellular / Remote Access Modal ---
async function openCellularModal() {
  const modal = document.getElementById("cellularModal");
  const qrImg = document.getElementById("cellularQrCode");
  const urlInput = document.getElementById("cellularUrlInput");

  modal.classList.remove("hidden");
  urlInput.value = window.location.origin;
  qrImg.src = `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(window.location.origin)}`;
  lucide.createIcons();
}

function closeCellularModal() {
  document.getElementById("cellularModal").classList.add("hidden");
}

function copyCellularUrl() {
  const urlInput = document.getElementById("cellularUrlInput");
  urlInput.select();
  navigator.clipboard.writeText(urlInput.value);
  const copyText = document.getElementById("copyText");
  copyText.innerText = "คัดลอกแล้ว!";
  setTimeout(() => {
    copyText.innerText = "คัดลอก";
  }, 2000);
}

// --- Initialization ---
window.addEventListener("DOMContentLoaded", async () => {
  updateNotificationButton();

  // Unlock iOS Safari Web Audio on first touch/click
  const unlockAudio = () => {
    getAudioContext();
    window.removeEventListener('touchstart', unlockAudio);
    window.removeEventListener('click', unlockAudio);
  };
  window.addEventListener('touchstart', unlockAudio, { passive: true });
  window.addEventListener('click', unlockAudio, { passive: true });

  // Click outside modal to close
  window.addEventListener("click", (e) => {
    const addModal = document.getElementById("addStockModal");
    const editModal = document.getElementById("editStockModal");
    const setModal = document.getElementById("settingsModal");
    const celModal = document.getElementById("cellularModal");
    if (e.target === addModal) closeAddStockModal();
    if (e.target === editModal) closeEditStockModal();
    if (e.target === setModal) closeSettingsModal();
    if (e.target === celModal) closeCellularModal();
  });

  // Check login state
  await checkAuthAndInit();
});
