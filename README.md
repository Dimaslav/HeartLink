# ❤️ HeartLink

HeartLink — Telegram-бот знакомств, написанный на Python с использованием aiogram 3.

Бот позволяет пользователям создавать анкеты, искать людей поблизости, ставить лайки, получать взаимные симпатии, покупать Premium через Telegram Stars и использовать административную панель.

---

## Возможности

### 👤 Анкета

- регистрация
- имя
- возраст
- пол
- кого ищет пользователь
- описание
- фотография
- геолокация
- редактирование анкеты

---

### 🔍 Поиск

- поиск случайных анкет
- фильтр по полу
- фильтр по возрасту
- поиск по расстоянию
- исключение уже просмотренных пользователей
- исключение заблокированных пользователей

---

### ❤️ Симпатии

- обычный лайк
- суперлайк
- взаимные симпатии
- просмотр входящих лайков
- отклонение лайка
- блокировка пользователя

---

### 🚨 Жалобы

- отправка жалобы
- хранение жалоб
- уведомление администратора

---

### ⭐ Premium

Поддерживается Telegram Stars.

Возможности Premium:

- дополнительные суперлайки
- Premium-статус
- покупка суперлайков

---

### 👨‍💻 Админ-панель

- статистика
- список пользователей
- рассылка
- выдача Premium

---

## Используемые технологии

- Python 3
- aiogram 3
- SQLite
- aiosqlite
- aiohttp
- Telegram Bot API

---

## Структура проекта

```
HeartLink/

├── config.py
├── database.py
├── ma.py
├── requirements.txt
└── bot.log
```

---

## Установка

Клонировать проект

```bash
git clone https://github.com/USERNAME/HeartLink.git
```

Перейти в папку

```bash
cd HeartLink
```

Установить зависимости

```bash
pip install -r requirements.txt
```

---

## Создать файл .env

Пример:

```env
BOT_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
ADMIN_ID=123456789
PAYMENT_PROVIDER_TOKEN=
PROXY_URL=
```

Если используются Telegram Stars, поле

```
PAYMENT_PROVIDER_TOKEN
```

можно оставить пустым.

---

## Запуск

```bash
python ma.py
```

---

## Deploy

Бот можно разместить на:

- Render
- Railway
- Oracle Cloud
- VPS

Для Render:

Build Command

```bash
pip install -r requirements.txt
```

Start Command

```bash
python ma.py
```

---

## Требуемые зависимости

Если файла `requirements.txt` нет или он устарел:

```text
aiogram==3.4.1
aiosqlite
aiohttp
python-dotenv
certifi
requests
Pillow
cachetools
```

---

## Лицензия

MIT License

---

## Автор

Разработчик: Dimaslav

GitHub:
https://github.com/Dimaslav
