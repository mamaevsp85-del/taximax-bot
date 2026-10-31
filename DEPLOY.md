# 🚀 Размещение на хостинге

## Вариант 1: Railway (рекомендуется)

### Шаг 1: Создать аккаунт
1. Перейти на [railway.app](https://railway.app)
2. Войти через GitHub

### Шаг 2: Создать проект
1. New Project → Deploy from GitHub repo
2. Выбрать репозиторий с проектом
3. Railway автоматически обнаружит Dockerfile

### Шаг 3: Настроить переменные окружения
В Railway Dashboard → Variables:
```
MAX_BOT_TOKEN=your_token
MAX_BASE_URL=https://platform-api.max.ru
ADMIN_USER_IDS=16155678,14566736
YANDEX_API_KEY=your_key
ENVIRONMENT=production
API_KEY=your_random_api_key
WEBHOOK_SECRET=optional_webhook_secret
STORAGE_FILE=/data/taxi_data.json
```
**Какие переменные реально читает приложение** (`config.py`, pydantic-settings):
`MAX_BOT_TOKEN`, `MAX_BASE_URL`, `ADMIN_USER_IDS`, `ADMIN_MAX_LINK`,
`SUPPORT_CONTACT`, `YANDEX_API_KEY`, `OPENROUTESERVICE_KEY`, `STORAGE_FILE`,
`SERVICE_AREA_FILE`, `ENVIRONMENT`, `DEBUG`, `LOG_LEVEL`, `SILENT_MODE`,
`API_KEY`, `WEBHOOK_SECRET`.
Переменные `SECRET_KEY`, `JWT_SECRET`, `STORAGE_BACKEND` — игнорируются
(`extra="ignore"`), НЕ задавайте их.
`WEBHOOK_SECRET` — если задан, webhook-приёмник `/se14566736_bot` требует
заголовок `X-Webhook-Secret` (или `?secret=`). MAX Bot API сам подпись
не присылает, поэтому секрет нужно прокидывать на уровне MAX/прокси или
полагаться на секретный путь + API-ключ.

### Шаг 4: Настроить том (volume)
1. Railway Dashboard → Settings → Volumes
2. Добавить volume: `/data`
3. Это сохранит данные между перезапусками

### Шаг 5: Получить URL
Railway выдаст URL: `https://your-app.railway.app`

### Шаг 6: Настроить MAX
1. MAX для партнёров → Чат-боты → Ваш бот → Настройки
2. Mini-App URL: `https://your-app.railway.app/`
3. Webhook URL: `https://your-app.railway.app/se14566736_bot`

---

## Вариант 2: Render

### Шаг 1: Создать аккаунт
1. Перейти на [render.com](https://render.com)
2. Войти через GitHub

### Шаг 2: Создать Web Service
1. New → Web Service
2. Подключить репозиторий
3. Render обнаружит Dockerfile

### Шаг 3: Настроить
- **Name:** taximax
- **Environment:** Docker
- **Plan:** Free
- **Environment Variables:** те же, что для Railway

### Шаг 4: Получить URL
Render выдаст URL: `https://taximax.onrender.com`

---

## Вариант 3: Vercel (только Mini-App)

### Для Mini-App (MaximusTaxi):
1. Перейти на [vercel.com](https://vercel.com)
2. Import Project → выбрать папку MaximusTaxi
3. Vercel автоматически разместит статические файлы
4. URL: `https://your-app.vercel.app`

---

## 📝 Чек-лист после размещения

- [ ] Бот запущен и отвечает на сообщения
- [ ] Mini-App открывается по HTTPS
- [ ] API бота доступен по HTTPS
- [ ] Mini-App делает запросы к API бота
- [ ] WebSocket работает
- [ ] Данные сохраняются (volume настроен)
- [ ] В MAX указаны правильные URL

---

## 🔧 Обновление кода

После изменений в коде:
```bash
git add .
git commit -m "Update"
git push
```
Railway/Render автоматически пересоберут и задеплоят.
