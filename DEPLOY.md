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
SECRET_KEY=your_secret_key
JWT_SECRET=your_jwt_secret
SILENT_MODE=True
API_KEY=your_api_key
ENVIRONMENT=production
STORAGE_BACKEND=file
STORAGE_FILE=/data/taxi_data.json
```

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
