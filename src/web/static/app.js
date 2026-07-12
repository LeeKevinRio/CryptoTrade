const { createApp, ref, reactive, computed, watch, onMounted, nextTick } = Vue;

// 從 localStorage 取 token；首次使用會 prompt
function getAuthToken() {
  let t = localStorage.getItem('web_auth_token');
  if (!t) {
    t = window.prompt('輸入 WEB_AUTH_TOKEN（後端啟動時印出的字串）：');
    if (t) localStorage.setItem('web_auth_token', t);
  }
  return t || '';
}

async function authFetch(url, options = {}) {
  const headers = options.headers || {};
  headers['X-Auth-Token'] = getAuthToken();
  if (options.body) headers['Content-Type'] = 'application/json';
  const r = await fetch(url, { ...options, headers });
  if (r.status === 401) {
    localStorage.removeItem('web_auth_token');
    alert('授權失敗，請重新整理頁面輸入 token');
  }
  return r;
}

createApp({
  setup() {
    const activeTab = ref('overview');     // overview | spot | futures
    const status = reactive({ testnet: true, symbols: [], timeframes: [], bots: [] });
    const overview = reactive({ total_balance: 0, today: {}, bots: [] });
    const perf = reactive({ overall: {}, diagnosis: [], by_symbol: [], by_reason: [], by_day: [] });
    const perfDays = ref(null);   // null = 全期間
    const perfPeriods = [
      { label: '全部', days: null },
      { label: '30天', days: 30 },
      { label: '7天', days: 7 },
    ];

    const sentiment = reactive({ symbols: {}, updated: 0 });
    const positions = ref([]);
    const recentTrades = ref([]);
    const signals = reactive({});
    const todayStats = reactive({ trades: 0, wins: 0, pnl: 0, win_rate: 0 });
    const risk = reactive({});
    const indicators = reactive({ symbol: '', funding_rate: 0, timeframes: {}, thresholds: {} });
    const events = ref([]);
    const wsConnected = ref(false);
    const selectedSymbol = ref('');
    const selectedTimeframe = ref('');
    const chartContainer = ref(null);

    const showForceModal = ref(false);
    const forceSymbol = ref('');
    const forceSide = ref('LONG');

    let chart = null;
    let candleSeries = null;
    let ws = null;
    let chartReqId = 0;
    let indReqId = 0;

    const activeBot = computed(() =>
      status.bots?.find((b) => b.bot_id === activeTab.value)
    );

    const signalList = computed(() => Object.values(signals));

    const indicatorRows = computed(() => {
      const tfs = indicators.timeframes || {};
      return Object.keys(tfs).map((tf) => ({ tf, ...tfs[tf] }));
    });

    const tradePctUsed = computed(() => {
      const max = risk.max_daily_trades || 1;
      const used = risk.daily_trades || 0;
      return Math.min(100, Math.round((used / max) * 100));
    });

    const filteredEvents = computed(() =>
      events.value.filter((e) => !e.bot || e.bot === activeTab.value)
    );

    const signalClass = (type) => `sig-${type || 'NEUTRAL'}`;

    const rsiClass = (tf, val) => {
      if (val == null) return '';
      const t = indicators.thresholds || {};
      const oversold = tf === '15m' ? (t.rsi_15m_long ?? 35) : (t.rsi_oversold ?? 30);
      const overbought = tf === '15m' ? (t.rsi_15m_short ?? 65) : (t.rsi_overbought ?? 70);
      if (val < oversold) return 'pnl-up cell-hot';
      if (val > overbought) return 'pnl-down cell-hot';
      if (val < oversold + 5) return 'pnl-up';
      if (val > overbought - 5) return 'pnl-down';
      return '';
    };

    const bbClass = (pos) => {
      if (pos == null) return '';
      if (pos <= 0.05) return 'pnl-up cell-hot';
      if (pos >= 0.95) return 'pnl-down cell-hot';
      return '';
    };

    const clampBBPos = (pos) => {
      if (pos == null) return 50;
      return Math.max(0, Math.min(1, pos)) * 100;
    };

    const volClass = (v) => {
      if (v == null) return '';
      const mult = indicators.thresholds?.volume_multiplier ?? 1.5;
      if (v >= mult) return 'pnl-up cell-hot';
      if (v < 0.5) return 'pnl-down';
      return '';
    };

    const formatHold = (sec) => {
      if (sec == null) return '—';
      if (sec < 60) return sec + 's';
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      if (m < 60) return `${m}m ${s}s`;
      const h = Math.floor(m / 60);
      return `${h}h ${m % 60}m`;
    };

    const fmtTime = () => {
      const d = new Date();
      return `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`;
    };

    // 把 ISO 時間字串格式化成 MM/DD HH:mm（本地時區）
    const fmtDateTime = (iso) => {
      if (!iso) return '—';
      const d = new Date(iso);
      if (isNaN(d.getTime())) return '—';
      const p = (n) => n.toString().padStart(2, '0');
      return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    };

    const pnlClass = (n) => {
      if (n === undefined || n === null) return '';
      if (n > 0) return 'pnl-up';
      if (n < 0) return 'pnl-down';
      return '';
    };

    const pushEvent = (type, msg, bot = null) => {
      events.value.push({ time: fmtTime(), type, msg, bot });
      if (events.value.length > 200) events.value.shift();
    };

    const fetchStatus = async () => {
      const r = await fetch('/api/status');
      const d = await r.json();
      Object.assign(status, d);
      if (!selectedSymbol.value && d.symbols?.length) selectedSymbol.value = d.symbols[0];
      if (!selectedTimeframe.value && d.timeframes?.length) {
        selectedTimeframe.value = d.timeframes.includes('5m') ? '5m' : d.timeframes[0];
      }
      if (!forceSymbol.value && d.symbols?.length) forceSymbol.value = d.symbols[0];
    };

    const fetchOverview = async () => {
      const r = await fetch('/api/overview');
      const d = await r.json();
      Object.assign(overview, d);
    };

    const fetchSentiment = async () => {
      try {
        const r = await fetch('/api/sentiment');
        if (!r.ok) return;
        const d = await r.json();
        sentiment.symbols = d.symbols || {};
        sentiment.updated = d.updated || 0;
      } catch (e) { console.error('fetchSentiment failed', e); }
    };

    const sentimentList = computed(() => Object.values(sentiment.symbols));

    const biasClass = (bias) => {
      if (bias == null) return '';
      if (bias >= 0.2) return 'pnl-up';
      if (bias <= -0.2) return 'pnl-down';
      return '';
    };

    const fetchPerformance = async () => {
      try {
        const q = perfDays.value != null ? `?days=${perfDays.value}` : '';
        const r = await fetch(`/api/performance${q}`);
        if (!r.ok) return;
        const d = await r.json();
        Object.assign(perf, d);
      } catch (e) {
        console.error('fetchPerformance failed', e);
      }
    };

    const setPerfDays = async (days) => {
      perfDays.value = days;
      await fetchPerformance();
    };

    const fetchBotData = async () => {
      if (activeTab.value === 'overview') return;
      const bot = activeTab.value;
      try {
        const [pr, sr, rr, tr] = await Promise.all([
          fetch(`/api/bots/${bot}/positions`).then((r) => r.json()),
          fetch(`/api/bots/${bot}/signals`).then((r) => r.json()),
          fetch(`/api/bots/${bot}/risk_budget`).then((r) => r.json()),
          fetch(`/api/bots/${bot}/trades`).then((r) => r.json()),
        ]);
        positions.value = pr;
        // 把 signals 變成 dict
        Object.keys(signals).forEach((k) => delete signals[k]);
        sr.forEach((s) => { signals[s.symbol] = s; });
        Object.assign(risk, rr);
        Object.assign(todayStats, tr.today || {});
        recentTrades.value = tr.recent || [];
      } catch (e) {
        console.error('fetchBotData failed', e);
      }
    };

    const fetchIndicators = async () => {
      if (activeTab.value === 'overview' || !selectedSymbol.value) return;
      const reqId = ++indReqId;
      try {
        const r = await fetch(`/api/bots/${activeTab.value}/indicators/${selectedSymbol.value}`);
        if (!r.ok) return;
        const d = await r.json();
        if (reqId !== indReqId) return;
        indicators.symbol = d.symbol;
        indicators.funding_rate = d.funding_rate;
        indicators.timeframes = d.timeframes || {};
        indicators.thresholds = d.thresholds || {};
      } catch (e) { console.error('indicators fetch failed', e); }
    };

    const initChart = () => {
      if (!chartContainer.value || chart) return;
      chart = LightweightCharts.createChart(chartContainer.value, {
        layout: { background: { color: '#161b22' }, textColor: '#d4d8df' },
        grid: { vertLines: { color: '#1a1f27' }, horzLines: { color: '#1a1f27' } },
        timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#2a313c' },
        rightPriceScale: { borderColor: '#2a313c' },
        crosshair: { mode: 1 },
      });
      candleSeries = chart.addCandlestickSeries({
        upColor: '#4ade80', downColor: '#f87171',
        borderUpColor: '#4ade80', borderDownColor: '#f87171',
        wickUpColor: '#4ade80', wickDownColor: '#f87171',
      });
      window.addEventListener('resize', () => {
        chart.applyOptions({ width: chartContainer.value.clientWidth });
      });
    };

    const loadChart = async () => {
      if (activeTab.value === 'overview') return;
      if (!selectedSymbol.value || !selectedTimeframe.value) return;
      const reqId = ++chartReqId;
      await nextTick();
      initChart();
      try {
        const r = await fetch(`/api/candles/${selectedSymbol.value}/${selectedTimeframe.value}?limit=300`);
        if (!r.ok) return;
        const data = await r.json();
        if (reqId !== chartReqId) return;  // 已被新請求覆蓋
        candleSeries.setData(data);
      } catch (e) { console.error('chart load failed', e); }
    };

    const onSymbolChange = async () => { await loadChart(); await fetchIndicators(); };

    const setTab = async (tab) => {
      activeTab.value = tab;
      // 切換時清空舊資料，避免短暫顯示前一個 bot 的數據誤導操作員
      positions.value = [];
      recentTrades.value = [];
      Object.keys(signals).forEach((k) => delete signals[k]);
      Object.keys(todayStats).forEach((k) => { todayStats[k] = 0; });
      Object.keys(risk).forEach((k) => delete risk[k]);
      indicators.timeframes = {};
      indicators.thresholds = {};
      // spot 不可做空，重設 forceSide
      if (tab === 'spot' && forceSide.value === 'SHORT') forceSide.value = 'LONG';
      if (tab !== 'overview') {
        await fetchBotData();
        await loadChart();
        await fetchIndicators();
      }
    };

    const closePosition = async (symbol) => {
      if (!confirm(`確定平倉 ${symbol}？`)) return;
      const r = await authFetch(`/api/bots/${activeTab.value}/actions/close`, {
        method: 'POST',
        body: JSON.stringify({ symbol }),
      });
      const d = await r.json();
      pushEvent('manual_close', `[${activeTab.value}] 平倉 ${symbol}`, activeTab.value);
      await fetchBotData();
    };

    const pause = async () => {
      await authFetch(`/api/bots/${activeTab.value}/actions/pause`, { method: 'POST' });
      await fetchStatus();
    };
    const resume = async () => {
      await authFetch(`/api/bots/${activeTab.value}/actions/resume`, { method: 'POST' });
      await fetchStatus();
    };

    const doForceTrade = async () => {
      try {
        const r = await authFetch(`/api/bots/${activeTab.value}/actions/force_trade`, {
          method: 'POST',
          body: JSON.stringify({ symbol: forceSymbol.value, side: forceSide.value }),
        });
        const d = await r.json();
        if (r.ok) {
          pushEvent('force_trade', `[${activeTab.value}] 強制 ${forceSide.value} ${forceSymbol.value}`, activeTab.value);
          await fetchBotData();
        } else {
          alert('失敗: ' + (d.detail || JSON.stringify(d)));
        }
      } catch (e) {
        alert('錯誤: ' + e.message);
      } finally {
        showForceModal.value = false;
      }
    };

    const connectWs = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => { wsConnected.value = true; };
      ws.onclose = () => {
        wsConnected.value = false;
        setTimeout(connectWs, 5000);
      };
      ws.onmessage = (msg) => {
        const event = JSON.parse(msg.data);
        handleEvent(event);
      };
    };

    const handleEvent = (event) => {
      const { type, data } = event;
      // bot-scoped events use "type.bot_id" pattern
      const [eventType, botId] = type.split('.');

      if (eventType === 'kline') {
        if (data.symbol === selectedSymbol.value &&
            data.interval === selectedTimeframe.value && candleSeries) {
          candleSeries.update({
            time: Math.floor(data.timestamp / 1000),
            open: data.open, high: data.high, low: data.low, close: data.close,
          });
        }
      } else if (eventType === 'signal') {
        if (botId === activeTab.value) signals[data.symbol] = data;
        if (data.actionable) {
          pushEvent('signal', `[${botId}] ${data.symbol} ${data.type} 強度=${data.strength.toFixed(1)}`, botId);
        }
      } else if (eventType === 'order_open') {
        pushEvent('order_open', `[${botId}] 開倉 ${data.symbol} ${data.side} @${data.entry_price}`, botId);
        if (botId === activeTab.value) fetchBotData();
        fetchStatus();
        fetchOverview();
      } else if (eventType === 'order_close') {
        pushEvent('order_close', `[${botId}] 平倉 ${data.symbol} pnl=${data.pnl} (${data.pnl_pct}%)`, botId);
        if (botId === activeTab.value) fetchBotData();
        fetchStatus();
        fetchOverview();
      } else if (eventType === 'paused') {
        fetchStatus();
      }
    };

    onMounted(async () => {
      await fetchStatus();
      await fetchOverview();
      await fetchPerformance();
      await fetchSentiment();
      connectWs();
      setInterval(fetchStatus, 30000);
      setInterval(fetchOverview, 15000);
      setInterval(fetchPerformance, 30000);
      setInterval(fetchSentiment, 60000);
      setInterval(() => {
        if (activeTab.value !== 'overview') {
          fetchBotData();
          fetchIndicators();
        }
      }, 8000);
    });

    return {
      activeTab, status, overview, activeBot,
      perf, perfDays, perfPeriods, setPerfDays,
      sentiment, sentimentList, biasClass, fetchSentiment,
      positions, recentTrades, signalList, todayStats, risk, indicators, events, filteredEvents,
      wsConnected, selectedSymbol, selectedTimeframe, chartContainer,
      showForceModal, forceSymbol, forceSide,
      indicatorRows, tradePctUsed,
      signalClass, rsiClass, bbClass, volClass, formatHold, clampBBPos,
      fmtDateTime, pnlClass,
      loadChart, onSymbolChange, setTab,
      closePosition, pause, resume, doForceTrade,
    };
  },
}).mount('#app');
