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

function query(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, value);
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

export const api = {
  dashboard: (day) => request(`/api/dashboard${query({ day })}`),
  daily: (day, search = '', scheme = '') => request(`/api/daily${query({ day, search, scheme })}`),
  timeseries: (from, to) => request(`/api/timeseries${query({ from, to })}`),
  alerts: (from, to) => request(`/api/alerts${query({ from, to })}`),
  products: (search = '') => request(`/api/products${query({ search })}`),
  addProduct: (product) => request('/api/products', { method: 'POST', body: JSON.stringify(product) }),
  updateProduct: (id, product) => request(`/api/products/${id}`, { method: 'PUT', body: JSON.stringify(product) }),
  ozonCatalog: () => request('/api/ozon/catalog-products'),
  bindProduct: (id, binding) => request(`/api/products/${id}/binding`, { method: 'PUT', body: JSON.stringify(binding) }),
  unbindProduct: (id) => request(`/api/products/${id}/binding`, { method: 'DELETE' }),
  updateNote: (day, productId, comment) => request(`/api/daily/${day}/${productId}/note`, { method: 'PUT', body: JSON.stringify({ comment }) }),
  costs: (day) => request(`/api/costs${query({ day })}`),
  addCost: (cost) => request('/api/costs', { method: 'POST', body: JSON.stringify(cost) }),
  updateCost: (id, cost) => request(`/api/costs/${id}`, { method: 'PUT', body: JSON.stringify(cost) }),
  deleteCost: (id) => request(`/api/costs/${id}`, { method: 'DELETE' }),
  updateEconomics: (id, values) => request(`/api/products/${id}/economics`, { method: 'PUT', body: JSON.stringify(values) }),
  status: () => request('/api/ozon/status'),
  syncRuns: () => request('/api/sync-runs'),
  connect: (credentials) => request('/api/ozon/connect', { method: 'POST', body: JSON.stringify(credentials) }),
  sync: ({ fast = true, from, to } = {}) => request(`/api/ozon/sync${query({ fast, from, to })}`, { method: 'POST' }),
  exportUrl: (day) => `${API_BASE}/api/export.xlsx${query({ day })}`,
};
