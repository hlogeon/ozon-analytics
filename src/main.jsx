import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {
  LayoutDashboard, CalendarDays, Package, Plus, Download, RefreshCw, Settings, Search,
  ChevronRight, TrendingUp, TrendingDown, TriangleAlert, CheckCircle2, X,
  Cloud, MoreHorizontal, ArrowUpRight, WalletCards, ShoppingBag, Percent, Target,
  Link2, Unlink, Trash2, Pencil, PlugZap,
} from 'lucide-react';
import './styles.css';
import {api} from './api';

const fmt = n => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2, minimumFractionDigits: 2}).format(n) + ' ₽';
const pct = n => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2, minimumFractionDigits: 2}).format(n) + '%';
const todayIso = () => new Intl.DateTimeFormat('en-CA', {timeZone: 'Europe/Moscow'}).format(new Date());
const shiftDay = (iso, delta) => {
  const date = new Date(`${iso}T00:00:00`);
  date.setDate(date.getDate() + delta);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
};
const formatDay = iso => new Date(`${iso}T00:00:00`).toLocaleDateString('ru-RU', {day: 'numeric', month: 'short'});
const formatWhen = value => {
  if (!value) return '';
  const parsed = new Date(value.includes('T') ? value : value.replace(' ', 'T') + 'Z');
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('ru-RU', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
};
const round2 = n => Math.round(n * 100) / 100;
const sumTotals = items => {
  const totals = items.reduce((acc, item) => ({
    sku_count: acc.sku_count + 1,
    sold: acc.sold + (item.sold || 0),
    revenue: acc.revenue + (item.revenue || 0),
    ads: acc.ads + (item.ads || 0),
    extra_costs: acc.extra_costs + (item.extra_costs || 0),
    profit: acc.profit + (item.profit || 0),
  }), {sku_count: 0, sold: 0, revenue: 0, ads: 0, extra_costs: 0, profit: 0});
  return {
    sku_count: totals.sku_count,
    sold: totals.sold,
    revenue: round2(totals.revenue),
    ads: round2(totals.ads),
    extra_costs: round2(totals.extra_costs),
    profit: round2(totals.profit),
    drr: totals.revenue ? round2((totals.ads / totals.revenue) * 100) : null,
    margin: totals.revenue ? round2((totals.profit / totals.revenue) * 100) : null,
  };
};

const SYNC_POLL_MS = 2000;
const SYNC_POLL_ATTEMPTS = 450;
// /api/ozon/sync returns 202 immediately, so completion is observed via status polling.
const waitForSync = async () => {
  for (let attempt = 0; attempt < SYNC_POLL_ATTEMPTS; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, SYNC_POLL_MS));
    const status = await api.status();
    if (!status.running) return status;
  }
  return null;
};

function App() {
  const [page, setPage] = useState('Главная');
  const [expense, setExpense] = useState(null);
  const [note, setNote] = useState(null);
  const [settings, setSettings] = useState(false);
  const [toast, setToast] = useState('');
  const [tab, setTab] = useState('Товары');
  const [dashboard, setDashboard] = useState(null);
  const [ozon, setOzon] = useState(null);
  const [day, setDay] = useState(todayIso());
  const [period, setPeriod] = useState(14);
  const [series, setSeries] = useState([]);
  const [alerts, setAlerts] = useState({items: [], total: 0});
  const [filter, setFilter] = useState(null);
  const [revision, setRevision] = useState(0);
  const notify = text => {
    setToast(text);
    setTimeout(() => setToast(''), 2500);
  };
  const loadStatus = () => api.status().then(setOzon).catch(error => notify(error.message));
  const loadDashboard = async selected => {
    try {
      const data = await api.dashboard(selected);
      setDashboard(data);
      if (data.day) setDay(data.day);
      return data;
    } catch (error) {
      notify(error.message);
      return null;
    }
  };
  const loadPeriod = async (endDay, days) => {
    if (!endDay) return;
    try {
      const from = shiftDay(endDay, -(days - 1));
      const [points, signals] = await Promise.all([api.timeseries(from, endDay), api.alerts(from, endDay)]);
      setSeries(points.items);
      setAlerts(signals);
    } catch (error) {
      notify(error.message);
    }
  };
  useEffect(() => {
    loadStatus();
    loadDashboard();
  }, []);
  useEffect(() => {
    if (day) loadPeriod(day, period);
  }, [day, period, revision]);
  useEffect(() => {
    if (!ozon?.running) return undefined;
    const timer = setInterval(async () => {
      const status = await api.status();
      setOzon(status);
      if (!status.running) {
        await loadDashboard(day);
        setRevision(value => value + 1);
      }
    }, 3000);
    return () => clearInterval(timer);
  }, [ozon?.running]);
  const refresh = async () => {
    try {
      const status = await api.status();
      setOzon(status);
      if (status.connected && !status.running) {
        await api.sync({fast: true});
        const started = await api.status();
        setOzon(started);
        notify('Синхронизация запущена');
      } else {
        await loadDashboard(day);
        setRevision(value => value + 1);
        notify('Данные обновлены');
      }
    } catch (error) {
      notify(error.message);
    }
  };
  const exportXlsx = () => {
    window.location.assign(api.exportUrl(day));
    notify('Отчёт Excel сформирован на сервере');
  };
  const saveExpense = async payload => {
    try {
      const {id, ...values} = payload;
      if (id) await api.updateCost(id, values);
      else await api.addCost(values);
      setExpense(null);
      await loadDashboard(values.day);
      setRevision(value => value + 1);
      notify('Расход сохранён, показатели пересчитаны');
    } catch (error) {
      notify(error.message);
    }
  };
  const saveNote = async comment => {
    try {
      await api.updateNote(note.day, note.product_id, comment);
      setNote(null);
      setRevision(value => value + 1);
      notify('Комментарий сохранён');
    } catch (error) {
      notify(error.message);
    }
  };
  const synced = async latestDay => {
    await loadStatus();
    await loadDashboard(latestDay || day);
    setRevision(value => value + 1);
  };
  const goPage = next => {
    setPage(next);
    setFilter(null);
  };
  const navigate = target => {
    if (target.filters?.kind === 'sync') {
      setSettings(true);
      return;
    }
    setPage(target.page);
    setFilter(target.filters || null);
    if (target.page === 'Ежедневный контроль') setTab('Товары');
  };
  return (
    <div className="app">
      <Sidebar page={page} setPage={goPage} connected={ozon?.connected} />
      <main>
        <Header
          page={page}
          ozon={ozon}
          onExpense={() => setExpense({})}
          onExport={exportXlsx}
          onSettings={() => setSettings(true)}
          onRefresh={refresh}
        />
        {page === 'Главная' ? (
          <Dashboard
            data={dashboard}
            ozon={ozon}
            period={period}
            series={series}
            alerts={alerts}
            onDayChange={loadDashboard}
            onPeriod={setPeriod}
            onNavigate={navigate}
          />
        ) : page === 'Ежедневный контроль' ? (
          <Daily
            tab={tab}
            setTab={setTab}
            day={day}
            revision={revision}
            notify={notify}
            onExpense={setExpense}
            onNote={setNote}
            initialFilter={filter}
          />
        ) : (
          <Catalog revision={revision} notify={notify} initialFilter={filter} />
        )}
      </main>
      {expense && <ExpenseModal close={() => setExpense(null)} save={saveExpense} day={day} initial={expense} />}
      {note && <NoteModal initial={note} close={() => setNote(null)} save={saveNote} />}
      {settings && <SettingsModal close={() => setSettings(false)} notify={notify} onSynced={synced} />}
      {toast && <div className="toast"><CheckCircle2 size={18} />{toast}</div>}
    </div>
  );
}

function Sidebar({page, setPage, connected}) {
  const nav = [['Главная', LayoutDashboard], ['Ежедневный контроль', CalendarDays], ['Товары и юнит-экономика', Package]];
  return (
    <aside>
      <div className="brand">
        <div className="logo">O</div>
        <div><b>Ozon Аналитика</b><small>Центр управления</small></div>
      </div>
      <nav>
        {nav.map(([name, Icon]) => (
          <button className={page === name ? 'active' : ''} onClick={() => setPage(name)} key={name}>
            <Icon size={19} /><span>{name}</span>{page === name && <i />}
          </button>
        ))}
      </nav>
      <div className="aside-bottom">
        <div className="sync">
          <div className="sync-icon"><Cloud size={18} /><span className={connected ? '' : 'off'} /></div>
          <div>
            <b>{connected ? 'Ozon подключён' : 'Ozon не подключён'}</b>
            <small>{connected ? 'Живые данные' : 'Укажите ключи в настройках'}</small>
          </div>
        </div>
        <div className="profile">
          <div className="avatar">АП</div>
          <div><b>Алексей Петров</b><small>Администратор</small></div>
          <MoreHorizontal size={18} />
        </div>
      </div>
    </aside>
  );
}

function Header({page, ozon, onExpense, onExport, onSettings, onRefresh}) {
  const connected = Boolean(ozon?.connected);
  const failed = ozon?.last_run?.status === 'error';
  const partial = ozon?.last_run?.status === 'partial';
  const running = Boolean(ozon?.running);
  let tone = 'warn';
  let title = 'Ozon не подключён.';
  let text = 'Укажите Client ID и API-ключ в настройках.';
  if (running) {
    tone = 'ok';
    title = 'Идёт синхронизация.';
    text = 'Показатели обновятся автоматически.';
  } else if (failed) {
    tone = 'error';
    title = 'Последняя синхронизация завершилась ошибкой.';
    text = ozon.last_run.error || 'Повторите обновление.';
  } else if (partial) {
    tone = 'warn';
    title = 'Синхронизация выполнена частично.';
    text = `Не все данные загружены: ${ozon.last_run.error || 'повторите обновление'}.`;
  } else if (connected) {
    tone = 'ok';
    title = 'Данные Ozon Seller API.';
    text = ozon.last_run?.finished_at
      ? `Последнее обновление: ${formatWhen(ozon.last_run.finished_at)}. Ручные расходы учтены в расчётах.`
      : 'Ручные расходы учтены в расчётах.';
  }
  return (
    <>
      <header>
        <div>
          <h1>{page}</h1>
          <p>
            {page === 'Главная'
              ? 'Обзор ключевых показателей вашего бизнеса'
              : page === 'Ежедневный контроль'
                ? 'Продажи, расходы и прибыль по товарам'
                : 'Каталог и параметры прибыльности товаров'}
          </p>
        </div>
        <div className="actions">
          <button className="icon-btn" onClick={onSettings}><Settings size={19} /></button>
          <button className="secondary" onClick={onExport}><Download size={18} />Экспорт Excel</button>
          <button className="primary" onClick={onExpense}><Plus size={18} />Добавить расход</button>
        </div>
      </header>
      <div className={`status-banner ${tone}`}>
        {connected ? <Cloud size={18} /> : <PlugZap size={18} />}
        <span><b>{title}</b> {text}</span>
        <button onClick={onRefresh}><RefreshCw size={15} />Обновить</button>
      </div>
    </>
  );
}

function EmptyState({connected, day, latestDay}) {
  const stale = connected && latestDay && day && latestDay !== day;
  return (
    <article className="panel empty-state">
      <PlugZap size={28} />
      <b>{connected ? 'Данных за период нет' : 'Подключите кабинет Ozon'}</b>
      <small>
        {stale
          ? `За ${formatDay(day)} данных пока нет. Последние загруженные данные: ${formatDay(latestDay)}. Нажмите «Обновить», чтобы подтянуть свежие заказы.`
          : connected
            ? 'За выбранную дату нет продаж. Выберите другой день или запустите синхронизацию.'
            : 'Укажите Client ID и API-ключ в настройках — показатели появятся после первой синхронизации.'}
      </small>
    </article>
  );
}

function Dashboard({data, ozon, period, series, alerts, onDayChange, onPeriod, onNavigate}) {
  const empty = !data || data.empty || !data.kpi;
  const kpi = data?.kpi;
  const previous = data?.previous;
  const delta = (key, suffix = '%') => {
    if (!kpi || !previous || !previous[key]) return '—';
    const value = key === 'margin' || key === 'drr' ? kpi[key] - previous[key] : (kpi[key] - previous[key]) / previous[key] * 100;
    return `${value < 0 ? '−' : '+'}${Math.abs(value).toFixed(2).replace('.', ',')}${suffix}`;
  };
  const cards = [
    ['Выручка', kpi ? fmt(kpi.revenue) : '—', delta('revenue'), kpi && kpi.revenue < (previous?.revenue ?? kpi.revenue) ? 'red' : 'green', WalletCards],
    ['Чистая прибыль', kpi ? fmt(kpi.profit) : '—', delta('profit'), kpi && kpi.profit < (previous?.profit ?? kpi.profit) ? 'red' : 'green', TrendingUp],
    ['Продано', kpi ? `${kpi.sold} шт.` : '—', delta('sold'), kpi && kpi.sold < (previous?.sold ?? kpi.sold) ? 'red' : 'green', ShoppingBag],
    ['Маржа', kpi && kpi.margin !== null ? pct(kpi.margin) : '—', delta('margin', ' п.п.'), kpi && kpi.margin < (previous?.margin ?? kpi.margin) ? 'red' : 'green', Percent],
    ['ДРР', kpi && kpi.drr !== null ? pct(kpi.drr) : '—', delta('drr', ' п.п.'), kpi && kpi.drr > (previous?.drr ?? kpi.drr) ? 'red' : 'green', Target],
  ];
  return (
    <div className="content">
      <div className="toolbar">
        <label className="date">
          <CalendarDays size={17} />
          <input type="date" value={data?.day || ''} onChange={event => onDayChange(event.target.value)} />
        </label>
        <div className="presets">
          {[7, 14, 30].map(days => (
            <button key={days} className={period === days ? 'selected' : ''} onClick={() => onPeriod(days)}>
              {days} дн.
            </button>
          ))}
        </div>
        <span className={`status ${ozon?.connected ? 'live' : ''}`}>
          <span />{ozon?.connected ? 'Ozon' : 'Не подключено'}
        </span>
        {data?.last_synced_at && (
          <span className="status">Обновлено в {formatWhen(data.last_synced_at)}</span>
        )}
      </div>
      <section className="kpis">
        {cards.map(([label, value, change, color, Icon]) => (
          <article className="kpi" key={label}>
            <div className="kpi-top"><span>{label}</span><div className="kpi-icon"><Icon size={19} /></div></div>
            <strong>{value}</strong>
            <div className={`change ${color}`}><TrendingDown />{change} <em>к предыдущему дню</em></div>
            {label === 'Чистая прибыль' && data?.status === 'preliminary' && <small className="pre">● предварительно</small>}
          </article>
        ))}
      </section>
      {empty ? <EmptyState connected={ozon?.connected} day={data?.day} latestDay={data?.latest_day} /> : (
        <div className="grid">
          <Chart items={series} />
          <Attention data={alerts} onNavigate={onNavigate} />
          <Leaders rows={data?.leaders || []} />
        </div>
      )}
    </div>
  );
}

function niceCeil(value) {
  if (value <= 0) return 1;
  const exp = 10 ** Math.floor(Math.log10(value));
  const scaled = value / exp;
  const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return nice * exp;
}

function formatAxis(value) {
  const abs = Math.abs(value);
  const sign = value < 0 ? '−' : '';
  if (abs >= 1_000_000) return `${sign}${(abs / 1_000_000).toFixed(1).replace('.0', '')} млн`;
  if (abs >= 1000) return `${sign}${Math.round(abs / 1000)} тыс.`;
  return `${sign}${Math.round(abs)}`;
}

function Chart({items}) {
  const [hover, setHover] = useState(null);
  const points = items || [];
  const hasData = points.some(item => item.revenue || item.profit || item.sold);
  if (!points.length || !hasData) {
    return (
      <article className="panel chart-panel">
        <div className="panel-head"><div><h2>Выручка и прибыль по дням</h2><p>Динамика за выбранный период</p></div></div>
        <div className="empty">Нет данных за выбранный период</div>
      </article>
    );
  }
  const width = 700;
  const height = 230;
  const pad = {left: 20, right: 20, top: 20, bottom: 10};
  const maxRaw = Math.max(...points.map(item => item.revenue), 0);
  const minRaw = Math.min(...points.map(item => item.profit), 0);
  const niceMax = niceCeil(maxRaw);
  const niceMin = minRaw < 0 ? -niceCeil(-minRaw) : 0;
  const span = niceMax - niceMin || 1;
  const x = index => (points.length === 1
    ? width / 2
    : pad.left + index * (width - pad.left - pad.right) / (points.length - 1));
  const y = value => pad.top + (1 - (value - niceMin) / span) * (height - pad.top - pad.bottom);
  const line = key => points.map((item, index) => `${index === 0 ? 'M' : 'L'}${x(index)} ${y(item[key])}`).join(' ');
  const lastX = x(points.length - 1);
  const area = `${line('revenue')} L${lastX} ${y(0)} L${x(0)} ${y(0)} Z`;
  const yTicks = [0, 1, 2, 3, 4].map(step => niceMax - span * step / 4);
  const xTicks = points.length <= 8
    ? points.map((item, index) => index)
    : [...new Set([0, Math.round((points.length - 1) / 3), Math.round(2 * (points.length - 1) / 3), points.length - 1])];
  const active = hover ?? points.length - 1;
  const tip = points[active];
  const onMove = event => {
    const box = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - box.left) / box.width;
    const index = Math.min(points.length - 1, Math.max(0, Math.round(ratio * (points.length - 1))));
    setHover(index);
  };
  return (
    <article className="panel chart-panel">
      <div className="panel-head">
        <div><h2>Выручка и прибыль по дням</h2><p>Динамика за выбранный период</p></div>
        <div className="legend"><span><i className="blue" />Выручка</span><span><i className="green-dot" />Прибыль</span></div>
      </div>
      <div className="chart">
        <div className="ylabels">{yTicks.map(value => <span key={value}>{formatAxis(value)}</span>)}</div>
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
          <defs>
            <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#1677ff" stopOpacity=".22" />
              <stop offset="1" stopColor="#1677ff" stopOpacity="0" />
            </linearGradient>
          </defs>
          <g className="lines">
            {yTicks.map(value => <path key={value} d={`M0 ${y(value)}H${width}`} />)}
          </g>
          <path className="area" d={area} />
          <path className="revenue-line" d={line('revenue')} />
          <path className="profit-line" d={line('profit')} />
          <circle cx={x(active)} cy={y(tip.revenue)} r="5" />
        </svg>
        <div className="xlabels">
          {xTicks.map(index => <span key={index}>{formatDay(points[index].day)}</span>)}
        </div>
        {tip && (
          <div className="tip">
            <b>{formatDay(tip.day)}</b>
            <span>Выручка <strong>{fmt(tip.revenue)}</strong></span>
            <span>Прибыль <strong>{fmt(tip.profit)}</strong></span>
          </div>
        )}
      </div>
    </article>
  );
}

function Attention({data, onNavigate}) {
  const [expanded, setExpanded] = useState(false);
  const items = data?.items || [];
  const total = data?.total || items.length;
  const visible = expanded ? items : items.slice(0, 4);
  const icons = {loss_makers: TrendingDown, high_drr: Target, missing_cost: TriangleAlert, unlinked: Unlink, sync_failed: TriangleAlert, sync_stale: RefreshCw};
  return (
    <article className="panel attention">
      <div className="panel-head">
        <div>
          <h2>Требует внимания</h2>
          <p>{total ? `${total} сигнал${total === 1 ? '' : total < 5 ? 'а' : 'ов'} за период` : 'Сигналов нет'}</p>
        </div>
        {items.length > 4 && (
          <button className="link" onClick={() => setExpanded(value => !value)}>
            {expanded ? 'Свернуть' : 'Все сигналы'} <ArrowUpRight size={15} />
          </button>
        )}
      </div>
      <div className="alerts">
        {visible.length ? visible.map(item => {
          const Icon = icons[item.code] || TriangleAlert;
          return (
            <button type="button" className="alert clickable" key={item.code} onClick={() => onNavigate(item.target)}>
              <div className={`alert-icon ${item.severity}`}><Icon size={17} /></div>
              <div><b>{item.title}</b><small>{item.subtitle}</small></div>
              <ChevronRight size={17} />
            </button>
          );
        }) : <div className="empty">Критических отклонений нет</div>}
      </div>
    </article>
  );
}

function Leaders({rows}) {
  return (
    <article className="panel leaders">
      <div className="panel-head"><div><h2>Эффективность товаров</h2><p>По чистой прибыли за выбранный день</p></div></div>
      {rows.length ? (
        <table>
          <thead><tr><th>ТОВАР</th><th>ПРОДАНО</th><th>ПРИБЫЛЬ</th><th>МАРЖА</th></tr></thead>
          <tbody>
            {rows.map((product, index) => (
              <tr key={product.product_id || product.sku}>
                <td><span className="rank">{index + 1}</span><div><b>{product.name}</b><small>SKU {product.sku}</small></div></td>
                <td>{product.sold} шт.</td>
                <td><b>{fmt(product.profit)}</b></td>
                <td><span className="margin">{product.margin === null ? '—' : pct(product.margin)}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <div className="empty">Нет продаж за выбранный день</div>}
    </article>
  );
}

function Daily({tab, setTab, day, revision, notify, onExpense, onNote, initialFilter}) {
  const [search, setSearch] = useState('');
  const [scheme, setScheme] = useState('');
  const [remote, setRemote] = useState(null);
  useEffect(() => {
    const timer = setTimeout(
      () => api.daily(day, search, scheme).then(setRemote).catch(error => notify(error.message)),
      150,
    );
    return () => clearTimeout(timer);
  }, [search, scheme, day, revision]);
  const alertFilter = initialFilter?.kind === 'loss' || initialFilter?.kind === 'high_drr';
  const rows = (remote?.items || []).filter(item => {
    if (initialFilter?.kind === 'loss') return item.profit < 0;
    if (initialFilter?.kind === 'high_drr') return item.drr !== null && item.drr > 20;
    return true;
  });
  const totals = alertFilter ? sumTotals(rows) : remote?.totals;
  return (
    <div className="content">
      <div className="tabs">
        <button className={tab === 'Товары' ? 'on' : ''} onClick={() => setTab('Товары')}>Товары</button>
        <button className={tab === 'Расходы' ? 'on' : ''} onClick={() => setTab('Расходы')}>Расходы</button>
      </div>
      <div className="filterbar">
        <span className="date"><CalendarDays size={17} />{day}</span>
        <label className="search"><Search size={17} /><input placeholder="Название или SKU" value={search} onChange={event => setSearch(event.target.value)} /></label>
        <select className="date" value={scheme} onChange={event => setScheme(event.target.value)}>
          <option value="">Все схемы</option>
          <option value="FBO">FBO</option>
          <option value="FBS">FBS</option>
        </select>
        {initialFilter?.kind && <span className="filter-chip">{initialFilter.kind === 'loss' ? 'Только убыточные' : 'ДРР выше 20%'}</span>}
      </div>
      {tab === 'Товары'
        ? <DataTable rows={rows} totals={totals} onExpense={onExpense} onNote={onNote} day={day} />
        : <Expenses day={day} revision={revision} notify={notify} onEdit={onExpense} />}
    </div>
  );
}

function DataTable({rows, totals, onExpense, onNote, day}) {
  const summary = totals || {sku_count: 0, sold: 0, revenue: 0, ads: 0, drr: null, extra_costs: 0, profit: 0, margin: null};
  return (
    <article className="panel data">
      <table>
        <thead>
          <tr>
            <th>ТОВАР И SKU</th><th>СХЕМА</th><th>ПРОДАНО</th><th>ВЫРУЧКА <small>API</small></th>
            <th>РЕКЛАМА <small className="manual">РУЧНОЙ</small></th><th>ДРР</th><th>ДОП. РАСХОДЫ</th>
            <th>ЧИСТАЯ ПРИБЫЛЬ</th><th>МАРЖА</th><th>КОММЕНТАРИЙ</th><th>СТАТУС</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(product => (
            <tr key={product.product_id}>
              <td><div><b>{product.name}</b><small>SKU {product.sku}</small></div></td>
              <td><span className="scheme">{product.scheme || '—'}</span></td>
              <td>{product.sold}</td>
              <td>{fmt(product.revenue)}</td>
              <td><button className="edit" onClick={() => onExpense({day, kind: 'ads', product_id: product.product_id})}>{fmt(product.ads)}</button></td>
              <td>{product.drr === null ? '—' : pct(product.drr)}</td>
              <td><button className="edit" onClick={() => onExpense({day, kind: 'extra', product_id: product.product_id})}>{fmt(product.extra_costs)}</button></td>
              <td className={product.profit < 0 ? 'negative' : ''}><b>{fmt(product.profit)}</b></td>
              <td>{product.margin === null ? '—' : pct(product.margin)}</td>
              <td><button className="note-button" onClick={() => onNote({...product, day})}>{product.note || 'Добавить'}</button></td>
              <td><span className={product.status === 'settled' ? 'ok-pill' : 'warn-pill'}>{product.status === 'settled' ? 'Сверено' : 'Предварительно'}</span></td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td><b>Итого · {summary.sku_count} SKU</b></td>
            <td /><td><b>{summary.sold}</b></td><td><b>{fmt(summary.revenue)}</b></td>
            <td><b>{fmt(summary.ads)}</b></td><td><b>{summary.drr === null ? '—' : pct(summary.drr)}</b></td>
            <td><b>{fmt(summary.extra_costs)}</b></td><td><b>{fmt(summary.profit)}</b></td>
            <td><b>{summary.margin === null ? '—' : pct(summary.margin)}</b></td><td /><td />
          </tr>
        </tfoot>
      </table>
    </article>
  );
}

function Expenses({day, revision, notify, onEdit}) {
  const [items, setItems] = useState([]);
  const load = () => api.costs(day).then(setItems).catch(error => notify(error.message));
  useEffect(() => { load(); }, [day, revision]);
  const remove = async id => {
    try {
      await api.deleteCost(id);
      await load();
      notify('Расход удалён');
    } catch (error) {
      notify(error.message);
    }
  };
  return (
    <article className="panel data">
      <table>
        <thead><tr><th>ДАТА</th><th>ТИП</th><th>ТОВАР</th><th>СУММА</th><th>КОММЕНТАРИЙ</th><th>АВТОР</th><th /></tr></thead>
        <tbody>
          {items.length ? items.map(item => (
            <tr key={item.id}>
              <td>{item.day}</td>
              <td><span className="scheme">{item.kind === 'ads' ? 'Реклама' : 'Доп. расход'}</span></td>
              <td>{item.product || 'Общий за день'}</td>
              <td><b>{fmt(item.amount)}</b></td>
              <td className="muted">{item.comment || '—'}</td>
              <td>{item.author}</td>
              <td>
                <button className="row-action" onClick={() => onEdit(item)} title="Редактировать"><Pencil size={15} /></button>
                <button className="row-action" onClick={() => remove(item.id)} title="Удалить"><Trash2 size={15} /></button>
              </td>
            </tr>
          )) : <tr><td colSpan="7" className="empty">Ручных расходов за этот день нет</td></tr>}
        </tbody>
      </table>
    </article>
  );
}

function Catalog({revision, notify, initialFilter}) {
  const [section, setSection] = useState(initialFilter?.kind === 'unlinked' ? 'Привязка' : 'Каталог');
  const [search, setSearch] = useState('');
  const [version, setVersion] = useState(0);
  const [productEditor, setProductEditor] = useState(null);
  const [economics, setEconomics] = useState(null);
  const [binding, setBinding] = useState(null);
  const [data, setData] = useState({items: [], summary: {active: 0, linked: 0, without_cost: 0}});
  const [ozonItems, setOzonItems] = useState([]);
  useEffect(() => {
    setSection(initialFilter?.kind === 'unlinked' ? 'Привязка' : 'Каталог');
  }, [initialFilter]);
  useEffect(() => {
    const timer = setTimeout(
      () => Promise.all([api.products(search), api.ozonCatalog()]).then(([productsData, ozonData]) => {
        setData(productsData);
        setOzonItems(ozonData);
      }).catch(error => notify(error.message)),
      150,
    );
    return () => clearTimeout(timer);
  }, [search, revision, version]);
  const reload = () => setVersion(value => value + 1);
  const saveProduct = async values => {
    try {
      if (productEditor?.id) await api.updateProduct(productEditor.id, values);
      else await api.addProduct(values);
      setProductEditor(null);
      reload();
      notify(productEditor?.id ? 'Товар обновлён' : 'Товар добавлен');
    } catch (error) {
      notify(error.message);
    }
  };
  const saveEconomics = async values => {
    try {
      await api.updateEconomics(economics.id, values);
      setEconomics(null);
      reload();
      notify('Юнит-экономика сохранена');
    } catch (error) {
      notify(error.message);
    }
  };
  const saveBinding = async values => {
    try {
      await api.bindProduct(binding.id, values);
      setBinding(null);
      reload();
      notify('SKU привязан к Ozon');
    } catch (error) {
      notify(error.message);
    }
  };
  const unbind = async product => {
    try {
      await api.unbindProduct(product.id);
      reload();
      notify('Привязка удалена');
    } catch (error) {
      notify(error.message);
    }
  };
  const visible = data.items.filter(product => {
    if (initialFilter?.kind === 'missing_cost') return !product.purchase_price;
    return true;
  });
  const lowMargin = data.items.filter(product => product.price && product.net_per_sale / product.price * 100 < 10).length;
  return (
    <div className="content">
      <div className="tabs catalog-tabs">
        <button className={section === 'Каталог' ? 'on' : ''} onClick={() => setSection('Каталог')}>Каталог и юнит-экономика</button>
        <button className={section === 'Привязка' ? 'on' : ''} onClick={() => setSection('Привязка')}>
          Привязка SKU к Ozon <span>{data.items.filter(product => product.source !== 'ozon' && product.mapping_status !== 'linked').length}</span>
        </button>
      </div>
      <section className="mini-kpis">
        <div><span>Активных SKU</span><b>{data.summary.active}</b></div>
        <div><span>Связано с Ozon</span><b>{data.summary.linked}</b><small>{data.summary.active ? Math.round(data.summary.linked / data.summary.active * 100) : 0}%</small></div>
        <div><span>Без себестоимости</span><b className="warn">{data.summary.without_cost}</b></div>
        <div><span>Маржа ниже порога</span><b className="bad">{lowMargin}</b></div>
      </section>
      {section === 'Каталог'
        ? <ProductCatalog data={{...data, items: visible}} search={search} setSearch={setSearch} onAdd={() => setProductEditor({})} onEdit={setProductEditor} onEconomics={setEconomics} filterKind={initialFilter?.kind} />
        : <BindingPage products={data.items.filter(product => product.source !== 'ozon')} onBind={setBinding} onUnbind={unbind} />}
      {productEditor && <ProductModal initial={productEditor} close={() => setProductEditor(null)} save={saveProduct} />}
      {economics && <EconomicsModal product={economics} close={() => setEconomics(null)} save={saveEconomics} />}
      {binding && <BindingModal product={binding} candidates={ozonItems.filter(item => item.id !== binding.id)} close={() => setBinding(null)} save={saveBinding} />}
    </div>
  );
}

function ProductCatalog({data, search, setSearch, onAdd, onEdit, onEconomics, filterKind}) {
  return (
    <>
      <div className="filterbar">
        <label className="search"><Search size={17} /><input placeholder="Найти товар или SKU" value={search} onChange={event => setSearch(event.target.value)} /></label>
        {filterKind === 'missing_cost' && <span className="filter-chip">Без себестоимости</span>}
        <button className="primary push-right" onClick={onAdd}><Plus size={16} />Добавить товар</button>
      </div>
      <article className="panel data">
        <table>
          <thead>
            <tr>
              <th>ТОВАР</th><th>SKU / OFFER_ID</th><th>OZON PRODUCT ID</th><th>ЦЕНА</th><th>СЕБЕСТОИМОСТЬ</th>
              <th>ЧИСТЫМИ С ПРОДАЖИ</th><th>БАЗОВАЯ МАРЖА</th><th>ROI</th><th>СВЯЗЬ С OZON</th><th />
            </tr>
          </thead>
          <tbody>
            {data.items.map(product => {
              const linked = product.mapping_status === 'linked';
              const margin = product.price ? product.net_per_sale / product.price * 100 : null;
              const roi = product.purchase_price ? product.net_per_sale / product.purchase_price * 100 : null;
              return (
                <tr key={product.id}>
                  <td><div><b>{product.name}</b><small>{product.scheme || '—'} · {product.source === 'manual' ? 'Ручной' : 'Ozon'}</small></div></td>
                  <td><code>{product.sku_original}</code><small>{product.offer_id ? `offer_id ${product.offer_id}` : ''}</small></td>
                  <td className="muted">{product.ozon_product_id || 'Не задан'}</td>
                  <td>{fmt(product.price)}</td>
                  <td>{product.purchase_price ? fmt(product.purchase_price) : <span className="bad-text">Не заполнена</span>}</td>
                  <td>{fmt(product.net_per_sale)}</td>
                  <td>{margin === null ? '—' : pct(margin)}</td>
                  <td>{roi === null ? '—' : pct(roi)}</td>
                  <td><span className={linked ? 'linked' : 'unlinked'}>{linked ? <Link2 size={13} /> : <Unlink size={13} />} {linked ? 'Связан' : 'Не связан'}</span></td>
                  <td className="row-actions">
                    <button className="row-action" onClick={() => onEdit(product)} title="Редактировать товар"><Pencil size={15} /></button>
                    <button className="row-action economy-action" onClick={() => onEconomics(product)} title="Юнит-экономика"><Percent size={15} /></button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </article>
    </>
  );
}

function BindingPage({products, onBind, onUnbind}) {
  return (
    <>
      <div className="section-help">
        <Link2 size={18} />
        <div>
          <b>Сопоставление локальных SKU с каталогом Ozon</b>
          <small>Выберите синхронизированный товар или укажите offer_id и product_id вручную.</small>
        </div>
      </div>
      <article className="panel data">
        <table>
          <thead><tr><th>ЛОКАЛЬНЫЙ ТОВАР</th><th>SKU</th><th>СТАТУС</th><th>OZON OFFER_ID</th><th>OZON PRODUCT ID</th><th>СХЕМА</th><th /></tr></thead>
          <tbody>
            {products.map(product => {
              const linked = product.mapping_status === 'linked' && (product.offer_id || product.ozon_product_id);
              return (
                <tr key={product.id}>
                  <td><b>{product.name}</b></td>
                  <td><code>{product.sku_original}</code></td>
                  <td><span className={linked ? 'linked' : 'unlinked'}>{linked ? <Link2 size={13} /> : <Unlink size={13} />} {linked ? 'Связан' : 'Ожидает привязки'}</span></td>
                  <td>{product.offer_id || '—'}</td>
                  <td>{product.ozon_product_id || '—'}</td>
                  <td>{product.scheme || '—'}</td>
                  <td className="row-actions">
                    <button className="secondary compact" onClick={() => onBind(product)}>{linked ? 'Изменить' : 'Привязать'}</button>
                    {linked && <button className="row-action" onClick={() => onUnbind(product)} title="Отвязать"><Unlink size={15} /></button>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </article>
    </>
  );
}

function Modal({children, close, title, sub}) {
  return (
    <div className="overlay" onMouseDown={event => event.target === event.currentTarget && close()}>
      <div className="modal">
        <div className="modal-head"><div><h2>{title}</h2><p>{sub}</p></div><button onClick={close}><X /></button></div>
        {children}
      </div>
    </div>
  );
}

function ProductModal({initial, close, save}) {
  const [name, setName] = useState(initial.name || '');
  const [sku, setSku] = useState(initial.sku_original || '');
  const [price, setPrice] = useState(initial.price ?? 0);
  const [scheme, setScheme] = useState(initial.scheme || '');
  return (
    <Modal close={close} title={initial.id ? 'Редактировать товар' : 'Новый товар'} sub="Ручная карточка товара и основной SKU">
      <div className="form">
        <label className="full">Название товара<input autoFocus value={name} onChange={event => setName(event.target.value)} placeholder="Например, Носки мужские 10 пар" /></label>
        <label>SKU<input value={sku} onChange={event => setSku(event.target.value)} placeholder="Уникальный SKU" /></label>
        <label>Схема<select value={scheme} onChange={event => setScheme(event.target.value)}><option value="">Не указана</option><option value="FBO">FBO</option><option value="FBS">FBS</option></select></label>
        <label>Цена, ₽<input type="number" min="0" step="0.01" value={price} onChange={event => setPrice(event.target.value)} /></label>
        <div className="form-hint">После сохранения заполните юнит-экономику и привяжите SKU к товару Ozon.</div>
      </div>
      <div className="modal-actions">
        <button onClick={close}>Отмена</button>
        <button className="primary" disabled={!name.trim() || !sku.trim()} onClick={() => save({name: name.trim(), sku_original: sku.trim(), price: Number(price), scheme: scheme || null})}>Сохранить товар</button>
      </div>
    </Modal>
  );
}

function BindingModal({product, candidates, close, save}) {
  const [target, setTarget] = useState('');
  const [offer, setOffer] = useState(product.offer_id || '');
  const [ozonId, setOzonId] = useState(product.ozon_product_id || '');
  const [scheme, setScheme] = useState(product.scheme || '');
  const choose = value => {
    setTarget(value);
    const item = candidates.find(candidate => String(candidate.id) === value);
    if (item) {
      setOffer(item.offer_id || item.sku || '');
      setOzonId(item.ozon_product_id || '');
      setScheme(item.scheme || scheme);
    }
  };
  return (
    <Modal close={close} title={`Привязка SKU ${product.sku_original}`} sub="Сопоставьте локальную позицию с товаром кабинета Ozon">
      <div className="form">
        <label className="full">Товар из последней синхронизации
          <select value={target} onChange={event => choose(event.target.value)}>
            <option value="">Ввести идентификаторы вручную</option>
            {candidates.map(item => <option key={item.id} value={item.id}>{item.name} · {item.offer_id || item.sku} · ID {item.ozon_product_id || '—'}</option>)}
          </select>
        </label>
        <label>Ozon offer_id<input value={offer} onChange={event => { setOffer(event.target.value); setTarget(''); }} placeholder="offer_id из Ozon" /></label>
        <label>Ozon product_id<input type="number" min="1" value={ozonId} onChange={event => { setOzonId(event.target.value); setTarget(''); }} placeholder="Например, 123456789" /></label>
        <label>Схема<select value={scheme} onChange={event => setScheme(event.target.value)}><option value="">Не указана</option><option value="FBO">FBO</option><option value="FBS">FBS</option></select></label>
        <div className="form-hint full">При выборе синхронизированного товара его продажи будут перенесены на локальный SKU.</div>
      </div>
      <div className="modal-actions">
        <button onClick={close}>Отмена</button>
        <button className="primary" disabled={!target && !offer.trim() && !ozonId} onClick={() => save({target_product_id: target ? Number(target) : null, offer_id: offer.trim() || null, ozon_product_id: ozonId ? Number(ozonId) : null, scheme: scheme || null})}>Сохранить привязку</button>
      </div>
    </Modal>
  );
}

function NoteModal({initial, close, save}) {
  const [comment, setComment] = useState(initial.note || '');
  return (
    <Modal close={close} title="Комментарий к показателям" sub={`${initial.name} · ${initial.day}`}>
      <div className="form single">
        <label className="full">Комментарий<textarea autoFocus value={comment} onChange={event => setComment(event.target.value)} placeholder="Изменения рекламы, причины отклонений и другие заметки" /></label>
        <div className="form-hint full">Пустой комментарий удалит существующую заметку.</div>
      </div>
      <div className="modal-actions">
        <button onClick={close}>Отмена</button>
        <button className="primary" onClick={() => save(comment)}>Сохранить комментарий</button>
      </div>
    </Modal>
  );
}

function ExpenseModal({close, save, day, initial}) {
  const [value, setValue] = useState(initial.amount || '');
  const [kind, setKind] = useState(initial.kind || 'extra');
  const [selectedDay, setSelectedDay] = useState(initial.day || day);
  const [productId, setProductId] = useState(initial.product_id || '');
  const [comment, setComment] = useState(initial.comment || '');
  const [catalog, setCatalog] = useState([]);
  useEffect(() => { api.products().then(data => setCatalog(data.items)); }, []);
  return (
    <Modal close={close} title={initial.id ? 'Редактировать расход' : 'Добавить расход'} sub="Показатели пересчитаются после сохранения">
      <div className="form">
        <label>Дата<input type="date" value={selectedDay} onChange={event => setSelectedDay(event.target.value)} /></label>
        <label>Тип расхода<select value={kind} onChange={event => setKind(event.target.value)}><option value="ads">Реклама</option><option value="extra">Дополнительный расход</option></select></label>
        <label>Сумма, ₽<input autoFocus type="number" min="0.01" step="0.01" placeholder="0,00" value={value} onChange={event => setValue(event.target.value)} /></label>
        <label>Товар <small>необязательно</small>
          <select value={productId} onChange={event => setProductId(event.target.value)}>
            <option value="">Общий расход за день</option>
            {catalog.map(product => <option key={product.id} value={product.id}>{product.name}</option>)}
          </select>
        </label>
        <label className="full">Комментарий<textarea placeholder="Например, съёмка контента" value={comment} onChange={event => setComment(event.target.value)} /></label>
      </div>
      <div className="modal-actions">
        <button onClick={close}>Отмена</button>
        <button className="primary" disabled={!value || Number(value) <= 0} onClick={() => save({id: initial.id, day: selectedDay, kind, amount: Number(value), product_id: productId ? Number(productId) : null, comment})}>Сохранить и пересчитать</button>
      </div>
    </Modal>
  );
}

function EconomicsModal({product, close, save}) {
  const fields = [
    ['purchase_price', 'Закупочная цена'], ['marking', 'Маркировка'], ['packaging', 'Упаковка и обработка'],
    ['inbound_delivery', 'Доставка до склада'], ['cross_dock', 'Кросс-док'], ['planned_logistics', 'Плановая логистика'],
    ['other_fixed', 'Прочие расходы'],
  ];
  const [values, setValues] = useState(Object.fromEntries(fields.map(([key]) => [key, product[key] ?? 0])));
  const [tax, setTax] = useState((product.tax_rate ?? 0.06) * 100);
  const [commission, setCommission] = useState((product.planned_commission ?? 0) * 100);
  const [validFrom, setValidFrom] = useState(new Date().toISOString().slice(0, 10));
  const update = (key, value) => setValues(current => ({...current, [key]: Number(value)}));
  return (
    <Modal close={close} title={product.name} sub={`SKU ${product.sku_original} · параметры юнит-экономики`}>
      <div className="form">
        {fields.map(([key, label]) => (
          <label key={key}>{label}, ₽<input type="number" min="0" step="0.01" value={values[key]} onChange={event => update(key, event.target.value)} /></label>
        ))}
        <label>Налоговая ставка, %<input type="number" min="0" max="100" step="0.01" value={tax} onChange={event => setTax(Number(event.target.value))} /></label>
        <label>Плановая комиссия, %<input type="number" min="0" max="100" step="0.01" value={commission} onChange={event => setCommission(Number(event.target.value))} /></label>
        <label>Действует с<input type="date" value={validFrom} onChange={event => setValidFrom(event.target.value)} /></label>
      </div>
      <div className="modal-actions">
        <button onClick={close}>Отмена</button>
        <button className="primary" onClick={() => save({...values, tax_rate: tax / 100, planned_commission: commission / 100, valid_from: validFrom})}>Сохранить</button>
      </div>
    </Modal>
  );
}

function SettingsModal({close, notify, onSynced}) {
  const [clientId, setClientId] = useState('');
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null);
  const [runs, setRuns] = useState([]);
  const load = () => Promise.all([api.status(), api.syncRuns()]).then(([next, history]) => {
    setStatus(next);
    setClientId(next.client_id || '');
    setRuns(history);
  });
  useEffect(() => { load().catch(error => notify(error.message)); }, []);
  const connect = async () => {
    setBusy(true);
    try {
      const next = await api.connect({client_id: clientId, api_key: key});
      setStatus(next);
      setKey('');
      notify('Кабинет Ozon подключён');
    } catch (error) {
      notify(error.message);
    } finally {
      setBusy(false);
    }
  };
  const sync = async () => {
    setBusy(true);
    try {
      await api.sync();
      await load();
      const finished = await waitForSync();
      await load();
      if (!finished) {
        notify('Синхронизация ещё выполняется, следите за статусом');
        return;
      }
      await onSynced();
      const run = finished.last_run;
      if (run?.status === 'error') notify(`Синхронизация не удалась: ${run.error || 'неизвестная ошибка'}`);
      else notify(`Синхронизация завершена: ${run?.records ?? 0} записей`);
    } catch (error) {
      await load();
      notify(error.message);
    } finally {
      setBusy(false);
    }
  };
  const schedule = status?.interval_minutes
    ? `Автосинхронизация каждые ${status.interval_minutes} мин.${status.next_run_at ? ` Следующий запуск: ${formatWhen(status.next_run_at)}.` : ''}`
    : 'Автосинхронизация выключена.';
  return (
    <Modal close={close} title="Подключение Ozon" sub="Seller API · только чтение">
      <div className="connection">
        <div className="conn-status">
          <div>
            <span className={`bigdot ${status?.connected ? 'on' : ''}`} />
            <b>{status?.connected ? 'Подключено' : 'Не подключено'}</b>
            <small>
              {status?.last_run?.finished_at
                ? `Последнее обновление: ${formatWhen(status.last_run.finished_at)}`
                : 'Подключите кабинет для получения живых данных'}
            </small>
            <small>{schedule}</small>
          </div>
        </div>
        <div className="form">
          <label>Client ID<input value={clientId} onChange={event => setClientId(event.target.value)} placeholder="Введите Client ID" /></label>
          <label>API Key<input value={key} onChange={event => setKey(event.target.value)} type="password" placeholder="••••••••••••••••" /></label>
        </div>
        <div className="secure">
          API-ключ хранится только на сервере и не передаётся в браузер.
          {runs[0]?.status === 'error' && ` Последняя ошибка: ${runs[0].error}`}
        </div>
      </div>
      <div className="modal-actions">
        <button onClick={close}>Закрыть</button>
        {status?.connected
          ? <button className="primary" disabled={busy} onClick={sync}>{busy ? 'Синхронизируем…' : 'Синхронизировать сейчас'}</button>
          : <button className="primary" disabled={busy || !clientId || !key} onClick={connect}>{busy ? 'Проверяем…' : 'Проверить подключение'}</button>}
      </div>
    </Modal>
  );
}

createRoot(document.getElementById('root')).render(<App />);
