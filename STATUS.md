# LXP Railway Bot: Статус и Описание Подключений

## 📂 Расположение файлов (C:/Users/kirpe/.openclaw/workspace/telegram/railway)
- `main.py` — Backend (FastAPI). Управляет заказами, оценкой стоимости, API для Mini App и Worker.
- `requirements.txt` — Список зависимостей Python (включая docx, pptx, openpyxl, pytest).
- `test_main.py` — Автотесты API на pytest.
- `Dockerfile` — Конфигурация сборки Docker для деплоя на Railway.
- `miniapp/index.html` — Фронтенд (разметка Mini App).
- `miniapp/app.js` — Фронтенд-логика (интеграция с Telegram WebApp API).
- `miniapp/style.css` — Стилизация Mini App.
- `PLAN.md` — Детальный план по фазам до финального идеала.
- `STATUS.md` — Данный файл (текущее состояние и гайд интеграций).

---

## 🔗 Подключения и Интеграции

### 1. GitHub
- **Репозиторий:** `https://github.com/kirill774/lxp_railway`
- **Текущая ветка:** `hardening/railway-deploy-security`
- **Команда синхронизации:**
  ```bash
  git add . && git commit -m "feat: stabilize api and plan next steps" && git push
  ```

### 2. Notion
- **Цель:** Использовать как CRM для трекинга заказов и базы ТЗ.
- **Подключение:** Требуется интеграционный токен Notion API (`NOTION_TOKEN`) и ID базы данных (`NOTION_DATABASE_ID`).
- **Где прописать:** В переменные окружения (env) на Railway.
- **Статус:** В планах на Phase 5.2.

### 3. Playwright
- **Цель:** UI автотесты для Mini App (проверка рендеринга и работы кнопок).
- **Подключение:** Node.js пакет `@playwright/test`.
- **Файл конфигурации:** `playwright.config.js` (будет создан в Phase 5.3).
- **Статус:** Ожидает развертывания Phase 5.

---

## 🚀 Что сейчас делаем
Мы завершили **Phase 1 (Стабилизация)**:
1. Защитили API от падений (502 / Upstream crash) с помощью глобальных обработчиков ошибок (Middleware) в FastAPI.
2. Расширили HealthCheck (`/api/health`), теперь он возвращает `"status": "healthy"`.
3. Добавили библиотеки для работы с файлами Office (`python-docx`, `python-pptx`, `openpyxl`) в `requirements.txt`.

---

## 📅 Что сделать в скором времени (Phase 2: Расширение профиля)
1. **База данных/Backend**: Расширить схему профиля пользователя в `main.py` (добавить поля академического уровня, предпочтительного формата и тональности).
2. **Интерфейс**: Отредактировать `miniapp/index.html` и `app.js`, чтобы дать пользователю возможность гибко настраивать эти параметры.
3. **Сохранение изменений**: Сделать коммит текущих файлов и запушить их в GitHub.
