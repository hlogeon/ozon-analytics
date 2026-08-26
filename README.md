# Ozon Аналитика — полнофункциональный MVP

MVP кабинета аналитики с React-интерфейсом, FastAPI backend, SQLite/PostgreSQL-совместимой моделью данных, реальным read-only адаптером Ozon Seller API и точным демо-набором за 1–2 июля 2026 года.

## Возможности

- 3 экрана: дашборд, ежедневный контроль, товары и юнит-экономика;
- 34 SKU и 68 нормализованных строк демо-данных;
- контрольные итоги исходной книги;
- ручные расходы с серверным хранением и пересчётом KPI;
- read-only Ozon adapter: проверка кабинета, товары, FBO/FBS отправления;
- идемпотентное сохранение отправлений и журнал синхронизаций;
- API Key никогда не возвращается frontend-клиенту;
- серверный Excel-экспорт текущего дня;
- Docker Compose для запуска всего приложения одной командой.

## Быстрый запуск через Docker

```bash
docker compose up --build
```

Интерфейс: <http://localhost:8080>, OpenAPI: <http://localhost:8080/api/docs>.

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

## Проверки

```bash
pytest -q
npm run build
```

База по умолчанию создаётся в `backend/analytics.db`. Путь можно изменить переменной `OZON_ANALYTICS_DB`.

## API

- `GET /api/dashboard` — KPI и лидеры;
- `GET /api/daily` — нормализованные строки `дата × SKU`;
- `GET /api/products` — товары и юнит-экономика;
- `GET/POST/DELETE /api/costs` — ручные расходы;
- `POST /api/ozon/connect` — проверка Client ID/API Key;
- `POST /api/ozon/sync` — read-only синхронизация;
- `GET /api/sync-runs` — последние запуски;
- `GET /api/export.xlsx` — Excel-экспорт.

Секрет Ozon хранится только в памяти backend-процесса. После перезапуска его необходимо ввести заново; это намеренно безопаснее хранения открытого ключа в SQLite.
