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
  dashboard: (day) => request(`/api/dashboard${day ? `?day=${day}` : ''}`),
  daily: (day = '2026-07-02', search = '', scheme = '') => request(`/api/daily?day=${day}&search=${encodeURIComponent(search)}&scheme=${scheme}`),
  products: (search = '') => request(`/api/products?search=${encodeURIComponent(search)}`),
  addProduct: (product) => request('/api/products', { method: 'POST', body: JSON.stringify(product) }),
  updateProduct: (id, product) => request(`/api/products/${id}`, { method: 'PUT', body: JSON.stringify(product) }),
  ozonCatalog: () => request('/api/ozon/catalog-products'),
  bindProduct: (id, binding) => request(`/api/products/${id}/binding`, { method: 'PUT', body: JSON.stringify(binding) }),
  unbindProduct: (id) => request(`/api/products/${id}/binding`, { method: 'DELETE' }),
  updateNote: (day, productId, comment) => request(`/api/daily/${day}/${productId}/note`, { method: 'PUT', body: JSON.stringify({ comment }) }),
  costs: (day) => request(`/api/costs${day ? `?day=${day}` : ''}`),
  addCost: (cost) => request('/api/costs', { method: 'POST', body: JSON.stringify(cost) }),
  updateCost: (id, cost) => request(`/api/costs/${id}`, { method: 'PUT', body: JSON.stringify(cost) }),
  deleteCost: (id) => request(`/api/costs/${id}`, { method: 'DELETE' }),
  updateEconomics: (id, values) => request(`/api/products/${id}/economics`, { method: 'PUT', body: JSON.stringify(values) }),
  status: () => request('/api/ozon/status'),
  syncRuns: () => request('/api/sync-runs'),
  connect: (credentials) => request('/api/ozon/connect', { method: 'POST', body: JSON.stringify(credentials) }),
  sync: () => request('/api/ozon/sync', { method: 'POST' }),
  exportUrl: (day) => `${API_BASE}/api/export.xlsx?day=${day}`,
};
