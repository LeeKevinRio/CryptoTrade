const { createApp, ref, reactive, computed, onMounted, nextTick } = Vue;

createApp({
  setup() {
    const status = reactive({
      testnet: true,
      paused: false,
      symbols: [],
      timeframes: [],
      balance: 0,
      started_at: "",
    });
    const positions = ref([]);
    const signals = reactive({});
    const todayStats = reactive({ trades: 0, wins: 0, pnl: 0, win_rate: 0 });
    const events = ref([]);
    const wsConnected = ref(false);
    const selectedSymbol = ref("");
    const selectedTimeframe = ref("");
    const chartContainer = ref(null);

    let chart = null;
    let candleSeries = null;
    let ws = null;

    const signalList = computed(() => Object.values(signals));

    const signalClass = (type) => `sig-${type || 'NEUTRAL'}`;

    const fmtTime = () => {
      const d = new Date();
      return `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`;
    };

    const pushEvent = (type, msg) => {
      events.value.push({ time: fmtTime(), type, msg });
      if (events.value.length > 100) events.value.shift();
    };

    const fetchStatus = async () => {
      const r = await fetch('/api/status');
      const d = await r.json();
      Object.assign(status, d);
      if (!selectedSymbol.value && d.symbols.length) selectedSymbol.value = d.symbols[0];
      if (!selectedTimeframe.value && d.timeframes.length) {
        selectedTimeframe.value = d.timeframes.includes('5m') ? '5m' : d.timeframes[0];
      }
    };

    const fetchPositions = async () => {
      const r = await fetch('/api/positions');
      positions.value = await r.json();
    };

    const fetchTrades = async () => {
      const r = await fetch('/api/trades');
      const d = await r.json();
      Object.assign(todayStats, d.today);
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
      if (!selectedSymbol.value || !selectedTimeframe.value) return;
      await nextTick();
      initChart();
      try {
        const r = await fetch(`/api/candles/${selectedSymbol.value}/${selectedTimeframe.value}?limit=300`);
        if (!r.ok) return;
        const data = await r.json();
        candleSeries.setData(data);
      } catch (e) {
        console.error('chart load failed', e);
      }
    };

    const closePosition = async (symbol) => {
      if (!confirm(`確定要緊急平倉 ${symbol}？`)) return;
      const r = await fetch('/api/actions/close', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol }),
      });
      const d = await r.json();
      pushEvent('manual_close', `手動平倉 ${symbol}: ${JSON.stringify(d.result)}`);
      await fetchPositions();
    };

    const pause = async () => {
      await fetch('/api/actions/pause', { method: 'POST' });
      status.paused = true;
    };

    const resume = async () => {
      await fetch('/api/actions/resume', { method: 'POST' });
      status.paused = false;
    };

    const connectWs = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => { wsConnected.value = true; pushEvent('paused', 'WebSocket 已連線'); };
      ws.onclose = () => {
        wsConnected.value = false;
        pushEvent('paused', 'WebSocket 斷線，5 秒後重連');
        setTimeout(connectWs, 5000);
      };
      ws.onmessage = (msg) => {
        const event = JSON.parse(msg.data);
        handleEvent(event);
      };
    };

    const handleEvent = (event) => {
      const { type, data } = event;
      if (type === 'kline') {
        if (data.symbol === selectedSymbol.value && data.interval === selectedTimeframe.value && candleSeries) {
          candleSeries.update({
            time: Math.floor(data.timestamp / 1000),
            open: data.open, high: data.high,
            low: data.low, close: data.close,
          });
        }
      } else if (type === 'signal') {
        signals[data.symbol] = data;
        if (data.actionable) pushEvent('signal', `${data.symbol} ${data.type} 強度=${data.strength.toFixed(1)}`);
      } else if (type === 'order_open') {
        pushEvent('order_open', `開倉 ${data.symbol} ${data.side} @${data.entry_price}`);
        fetchPositions();
        fetchStatus();
      } else if (type === 'order_close') {
        pushEvent('order_close', `平倉 ${data.symbol} pnl=${data.pnl} (${data.pnl_pct}%)`);
        fetchPositions();
        fetchTrades();
        fetchStatus();
      } else if (type === 'paused') {
        status.paused = data.paused;
      }
    };

    onMounted(async () => {
      await fetchStatus();
      await fetchPositions();
      await fetchTrades();
      await loadChart();
      connectWs();
      setInterval(fetchPositions, 5000);
      setInterval(fetchTrades, 15000);
      setInterval(fetchStatus, 30000);
    });

    return {
      status, positions, signalList, todayStats, events, wsConnected,
      selectedSymbol, selectedTimeframe, chartContainer,
      signalClass, loadChart, closePosition, pause, resume,
    };
  },
}).mount('#app');
