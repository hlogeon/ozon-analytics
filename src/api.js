const API_BASE = import.meta.env.VITE_API_URL || '';

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}

export const api = {
  dashboard: (day = '2026-07-02') => request(`/api/dashboard?day=${day}`),
  daily: (day = '2026-07-02', search = '') => request(`/api/daily?day=${day}&search=${encodeURIComponent(search)}`),
  products: () => request('/api/products'),
  addCost: (cost) => request('/api/costs', { method: 'POST', body: JSON.stringify(cost) }),
  status: () => request('/api/ozon/status'),
  connect: (credentials) => request('/api/ozon/connect', { method: 'POST', body: JSON.stringify(credentials) }),
  sync: () => request('/api/ozon/sync', { method: 'POST' }),
  exportUrl: `${API_BASE}/api/export.xlsx?day=2026-07-02`,
};
