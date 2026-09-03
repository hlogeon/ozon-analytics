# Ozon Аналитика — MVP

Кабинет аналитики: React + FastAPI + SQLite, read-only Ozon Seller API, инкрементальная синхронизация.

## Возможности

- 3 экрана: дашборд, ежедневный контроль, товары и юнит-экономика;
- график выручки и прибыли и сигналы «Требует внимания» из живых данных;
- ручные расходы с серверным хранением и пересчётом KPI;
- read-only Ozon adapter: проверка кабинета, товары, FBO/FBS отправления, финансы;
- инкрементальная синхронизация с перекрытием 3 дней и фоновый планировщик;
- API Key никогда не возвращается frontend-клиенту;
- серверный Excel-экспорт текущего дня;
- Docker Compose для запуска всего приложения одной командой.

## Быстрый запуск через Docker

```bash
docker compose up --build
```

Интерфейс: <http://localhost:8085>, OpenAPI: <http://localhost:8085/api/docs>.

## Запуск для разработки

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

В другом терминале:

```bash
npm install
npm run dev
```

Vite проксирует `/api` на `localhost:8000`.

Ключи Ozon: `OZON_SELLER_ID` / `OZON_CLIENT_ID` и `OZON_API_KEY`. Интервал фоновой синхронизации: `OZON_SYNC_INTERVAL_MINUTES` (по умолчанию 30, `0` — выключить).

## Проверки

```bash
pytest -q
npm run build
```

База по умолчанию создаётся в `backend/analytics.db`. Путь можно изменить переменной `OZON_ANALYTICS_DB`.

## API

- `GET /api/dashboard` — KPI и лидеры;
- `GET /api/daily` — нормализованные строки `дата × SKU`;
- `GET /api/timeseries` — выручка и прибыль по дням;
- `GET /api/alerts` — сигналы, требующие внимания;
- `GET /api/products` — товары и юнит-экономика;
- `GET/POST/DELETE /api/costs` — ручные расходы;
- `POST /api/ozon/connect` — проверка Client ID/API Key;
- `POST /api/ozon/sync` — read-only синхронизация;
- `GET /api/sync-runs` — последние запуски;
- `GET /api/export.xlsx` — Excel-экспорт.

Секрет Ozon хранится только в памяти backend-процесса. После перезапуска его необходимо ввести заново, если нет `.env`.
