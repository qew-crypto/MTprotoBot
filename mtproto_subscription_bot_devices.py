#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот подписки на MTProto-прокси.

Перед первым запуском измените только две строки ниже:
BOT_TOKEN и ADMIN_ID. После этого нажмите «Запуск» в панели хостинга.

Возможности:
- подписка на 30 дней;
- Telegram Stars;
- xRocket (включается после настройки токена и суммы администратором);
- отдельный MTProto-ключ для каждого устройства;
- реальное отключение ключа устройства через перезапуск MTProxy;
- выдача доступа только при активной подписке;
- ручная выдача/отзыв подписки;
- кнопка техподдержки @hawkuy;
- Telegram-админ-панель;
- SQLite, без отдельной базы данных;
- автоматическая установка aiogram/aiohttp.
"""

# ================= ОБЯЗАТЕЛЬНО ЗАПОЛНИТЬ =================
BOT_TOKEN = "8908305940:AAGLAce2S1BJHuAews4V21Z9ARf4-cFmz0c"
ADMIN_ID = 1842295433  # Ваш числовой Telegram ID
# =========================================================

import asyncio
import importlib.util
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def install_dependencies():
    missing = [p for p in ("aiogram", "aiohttp") if importlib.util.find_spec(p) is None]
    if missing:
        print("Устанавливаю зависимости:", ", ".join(missing), flush=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "aiogram>=3.7,<4", "aiohttp>=3.9,<4"])
        os.execv(sys.executable, [sys.executable, *sys.argv])


install_dependencies()

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.client.default import DefaultBotProperties

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "subscription_bot.sqlite3"
router = Router()


def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            joined_at INTEGER NOT NULL,
            subscription_until INTEGER NOT NULL DEFAULT 0,
            banned INTEGER NOT NULL DEFAULT 0,
            awaiting_support INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            external_id TEXT,
            amount TEXT,
            currency TEXT,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            payload TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            secret TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );
        """)
        defaults = {
            "price_rub": "100",
            "subscription_days": "30",
            "stars_amount": "100",
            "support_username": "hawkuy",
            "server_host": "",
            "server_port": "443",
            "device_limit": "3",
            "xrocket_token": "",
            "xrocket_amount": "",
            "xrocket_currency": "USDT",
            "xrocket_api": "https://pay.xrocket.tg",
        }
        con.executemany(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            defaults.items(),
        )


def get_setting(key, default=""):
    with db_connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with db_connect() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def upsert_user(message_or_user):
    u = message_or_user.from_user if hasattr(message_or_user, "from_user") else message_or_user
    if not u:
        return
    with db_connect() as con:
        con.execute(
            """INSERT INTO users(user_id,username,full_name,joined_at)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
            (u.id, u.username or "", u.full_name or "", int(time.time())),
        )


def get_user(user_id):
    with db_connect() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def is_admin(user_id):
    return user_id == ADMIN_ID


def subscription_active(user_id):
    row = get_user(user_id)
    return bool(row and not row["banned"] and row["subscription_until"] > int(time.time()))


def grant_subscription(user_id, days):
    now = int(time.time())
    with db_connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO users(user_id,username,full_name,joined_at) VALUES(?,?,?,?)",
            (user_id, "", "", now),
        )
        row = con.execute("SELECT subscription_until FROM users WHERE user_id=?", (user_id,)).fetchone()
        start = max(now, int(row["subscription_until"] or 0))
        until = start + int(days) * 86400
        con.execute("UPDATE users SET subscription_until=? WHERE user_id=?", (until, user_id))
    return until


def format_until(ts):
    if not ts:
        return "нет"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def active_devices(user_id=None):
    with db_connect() as con:
        if user_id is None:
            return con.execute("SELECT * FROM devices WHERE active=1 ORDER BY id").fetchall()
        return con.execute("SELECT * FROM devices WHERE user_id=? AND active=1 ORDER BY id", (user_id,)).fetchall()


def device_link(secret):
    host = get_setting("server_host")
    port = get_setting("server_port", "443")
    return f"tg://proxy?server={host}&port={port}&secret={secret}"


def ensure_mtproxy_installed():
    """Install and build the official MTProxy automatically when missing."""
    binary = Path("/opt/mtproxy/objs/bin/mtproto-proxy")
    proxy_secret = Path("/etc/mtproxy/proxy-secret")
    proxy_config = Path("/etc/mtproxy/proxy-multi.conf")
    if binary.exists() and proxy_secret.exists() and proxy_config.exists():
        return
    if os.geteuid() != 0:
        raise RuntimeError("для автоматической установки MTProxy запустите бота с правами root")
    if not Path("/usr/bin/apt-get").exists():
        raise RuntimeError("автоустановка поддерживает Ubuntu/Debian с apt-get")

    print("MTProxy не найден — начинаю автоматическую установку...", flush=True)
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    subprocess.run(["apt-get", "update"], check=True, env=env)
    subprocess.run([
        "apt-get", "install", "-y", "git", "make", "gcc", "g++",
        "libssl-dev", "zlib1g-dev", "curl", "ca-certificates"
    ], check=True, env=env)

    install_dir = Path("/opt/mtproxy")
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    if (install_dir / ".git").exists():
        subprocess.run(["git", "-C", str(install_dir), "fetch", "--depth", "1", "origin"], check=True)
        subprocess.run(["git", "-C", str(install_dir), "reset", "--hard", "origin/master"], check=True)
    elif install_dir.exists():
        raise RuntimeError("/opt/mtproxy существует, но это не репозиторий MTProxy; удалите или переименуйте каталог")
    else:
        subprocess.run([
            "git", "clone", "--depth", "1",
            "https://github.com/TelegramMessenger/MTProxy.git", str(install_dir)
        ], check=True)
    subprocess.run(["make", "-j2"], cwd=str(install_dir), check=True)
    if not binary.exists():
        raise RuntimeError("MTProxy не собрался: исполняемый файл не найден")

    config_dir = Path("/etc/mtproxy")
    config_dir.mkdir(parents=True, exist_ok=True)
    for url, destination in (
        ("https://core.telegram.org/getProxySecret", proxy_secret),
        ("https://core.telegram.org/getProxyConfig", proxy_config),
    ):
        request = urllib.request.Request(url, headers={"User-Agent": "mtproxy-bot-installer/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        if not data:
            raise RuntimeError(f"Telegram вернул пустой конфигурационный файл: {url}")
        destination.write_bytes(data)
        os.chmod(destination, 0o600)

    port = get_setting("server_port", "443")
    if Path("/usr/sbin/ufw").exists() or Path("/usr/bin/ufw").exists():
        subprocess.run(["ufw", "allow", f"{port}/tcp"], check=False)
    print("MTProxy установлен.", flush=True)


def _stop_direct_mtproxy():
    """Stop the MTProxy child used on hosting without systemd."""
    pid_file = BASE_DIR / "mtproxy.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    finally:
        pid_file.unlink(missing_ok=True)


def sync_mtproxy_service():
    """Apply device secrets using systemd or a direct child process."""
    if os.geteuid() != 0:
        raise RuntimeError("бот должен быть запущен с правами root для управления MTProxy")
    ensure_mtproxy_installed()
    binary = Path("/opt/mtproxy/objs/bin/mtproto-proxy")
    proxy_secret = Path("/etc/mtproxy/proxy-secret")
    proxy_config = Path("/etc/mtproxy/proxy-multi.conf")
    secrets_list = [row["secret"] for row in active_devices()]
    systemctl = shutil.which("systemctl")

    if not secrets_list:
        if systemctl:
            subprocess.run([systemctl, "stop", "mtproxy.service"], check=False)
        _stop_direct_mtproxy()
        return

    port = int(get_setting("server_port", "443"))
    common_args = [
        str(binary), "-u", "nobody", "-p", "8888", "-H", str(port)
    ]
    for value in secrets_list:
        common_args.extend(["-S", value])
    common_args.extend(["--aes-pwd", str(proxy_secret), str(proxy_config), "-M", "1"])

    if systemctl:
        secret_args = " ".join(f"-S {value}" for value in secrets_list)
        service = (
            "[Unit]\nDescription=Telegram MTProto Proxy (device access)\n"
            "Wants=network-online.target\nAfter=network-online.target\n\n"
            "[Service]\nType=simple\nUser=root\nWorkingDirectory=/opt/mtproxy\n"
            f"ExecStart={binary} -u nobody -p 8888 -H {port} {secret_args} --aes-pwd {proxy_secret} {proxy_config} -M 1\n"
            "Restart=on-failure\nRestartSec=5\nLimitNOFILE=65535\n"
            "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=full\nProtectHome=true\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        Path("/etc/systemd/system/mtproxy.service").write_text(service)
        subprocess.run([systemctl, "daemon-reload"], check=True)
        subprocess.run([systemctl, "enable", "--now", "mtproxy.service"], check=True)
        subprocess.run([systemctl, "restart", "mtproxy.service"], check=True)
        return

    # Контейнерные панели для ботов часто не имеют systemd. В этом случае
    # MTProxy запускается дочерним процессом и живёт вместе с ботом.
    _stop_direct_mtproxy()
    log_path = BASE_DIR / "mtproxy.log"
    log_handle = open(log_path, "ab", buffering=0)
    process = subprocess.Popen(
        common_args,
        cwd="/opt/mtproxy",
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (BASE_DIR / "mtproxy.pid").write_text(str(process.pid))
    time.sleep(1)
    if process.poll() is not None:
        log_handle.close()
        tail = ""
        try:
            tail = log_path.read_text(errors="replace")[-1500:]
        except Exception:
            pass
        (BASE_DIR / "mtproxy.pid").unlink(missing_ok=True)
        raise RuntimeError(f"MTProxy завершился сразу после запуска. Лог: {tail}")
    log_handle.close()


async def expire_devices_loop():
    while True:
        await asyncio.sleep(60)
        now = int(time.time())
        with db_connect() as con:
            expired = con.execute(
                """SELECT d.id FROM devices d JOIN users u ON u.user_id=d.user_id
                   WHERE d.active=1 AND (u.subscription_until<=? OR u.banned=1)""",
                (now,),
            ).fetchall()
            if expired:
                con.executemany("UPDATE devices SET active=0 WHERE id=?", [(r["id"],) for r in expired])
        if expired:
            try:
                await asyncio.to_thread(sync_mtproxy_service)
            except Exception as exc:
                print("Ошибка отключения просроченных устройств:", exc, flush=True)


def main_keyboard(user_id):
    rows = [
        [InlineKeyboardButton(text="🔗 Получить ссылку", callback_data="proxy")],
        [InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy")],
        [InlineKeyboardButton(text="👤 Моя подписка", callback_data="profile")],
        [InlineKeyboardButton(text="📱 Мои устройства", callback_data="devices")],
        [InlineKeyboardButton(text="🆘 Техподдержка @hawkuy", url="https://t.me/hawkuy")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard(target="home"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
    ])


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🌐 Настройка сервера", callback_data="admin_proxy")],
        [InlineKeyboardButton(text="💰 Цены", callback_data="admin_prices")],
        [InlineKeyboardButton(text="🚀 xRocket", callback_data="admin_xrocket")],
        [InlineKeyboardButton(text="🎁 Выдать подписку", callback_data="admin_grant")],
        [InlineKeyboardButton(text="⛔ Отозвать подписку", callback_data="admin_revoke")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
    ])


async def safe_edit(call, text, keyboard=None):
    try:
        await call.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)
    await call.answer()


@router.message(CommandStart())
async def start(message: Message):
    upsert_user(message)
    row = get_user(message.from_user.id)
    if row and row["banned"]:
        return await message.answer("Доступ к боту ограничен.")
    await message.answer(
        f"Добро пожаловать! Подписка на MTProto-прокси — <b>{get_setting('price_rub')} ₽ за {get_setting('subscription_days')} дней</b>.",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    upsert_user(call)
    await safe_edit(call, "Главное меню:", main_keyboard(call.from_user.id))


@router.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    upsert_user(call)
    row = get_user(call.from_user.id)
    active = subscription_active(call.from_user.id)
    text = (
        f"👤 <b>Моя подписка</b>\n\n"
        f"Статус: {'✅ активна' if active else '❌ не активна'}\n"
        f"Действует до: {format_until(row['subscription_until'])}"
    )
    await safe_edit(call, text, back_keyboard())


@router.callback_query(F.data == "proxy")
async def proxy(call: CallbackQuery):
    upsert_user(call)
    if not subscription_active(call.from_user.id):
        return await safe_edit(call, "Сначала оформите или продлите подписку.", back_keyboard("buy"))
    devices = active_devices(call.from_user.id)
    if not devices:
        return await safe_edit(call, "У вас пока нет подключённого устройства. Создайте его командой:\n<code>/add_device Телефон</code>", back_keyboard("devices"))
    device = devices[0]
    link = device_link(device["secret"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"➕ Подключить: {device['name']}", url=link)],
        [InlineKeyboardButton(text="📱 Все устройства", callback_data="devices")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ])
    await safe_edit(call, f"🔗 Ссылка устройства <b>{device['name']}</b>:\n<code>{link}</code>\n\nУ каждого устройства должен быть отдельный доступ.", kb)


@router.callback_query(F.data == "devices")
async def devices_menu(call: CallbackQuery):
    upsert_user(call)
    if not subscription_active(call.from_user.id):
        return await safe_edit(call, "Управление устройствами доступно только по активной подписке.", back_keyboard("buy"))
    rows = []
    for device in active_devices(call.from_user.id):
        rows.append([
            InlineKeyboardButton(text=f"🔗 {device['name']}", url=device_link(device["secret"])),
            InlineKeyboardButton(text="❌ Отключить", callback_data=f"revoke_device:{device['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить устройство", callback_data="device_add_help")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home")])
    await safe_edit(call, "📱 <b>Мои устройства</b>\n\nОтключение удаляет уникальный ключ устройства из MTProxy. Повторно подключиться по старой ссылке будет нельзя.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "device_add_help")
async def device_add_help(call: CallbackQuery):
    await safe_edit(call, "Добавьте устройство командой, например:\n<code>/add_device Телефон</code>\n<code>/add_device Ноутбук</code>", back_keyboard("devices"))


@router.message(Command("add_device"))
async def add_device_command(message: Message):
    upsert_user(message)
    if not subscription_active(message.from_user.id):
        return await message.answer("Нужна активная подписка.", reply_markup=main_keyboard(message.from_user.id))
    host = get_setting("server_host")
    if not host:
        return await message.answer("Сервер ещё не настроен администратором. Поддержка: @hawkuy")
    name = message.text.partition(" ")[2].strip()[:32]
    if not name:
        return await message.answer("Пример: <code>/add_device Телефон</code>")
    devices = active_devices(message.from_user.id)
    limit = int(get_setting("device_limit", "3"))
    if len(devices) >= limit:
        return await message.answer(f"Достигнут лимит: {limit} устройства. Сначала отключите одно из старых.")
    secret = __import__("secrets").token_hex(16)
    with db_connect() as con:
        cursor = con.execute(
            "INSERT INTO devices(user_id,name,secret,active,created_at) VALUES(?,?,?,?,?)",
            (message.from_user.id, name, secret, 1, int(time.time())),
        )
        device_id = cursor.lastrowid
    try:
        await asyncio.to_thread(sync_mtproxy_service)
    except Exception as exc:
        with db_connect() as con:
            con.execute("UPDATE devices SET active=0 WHERE id=?", (device_id,))
        return await message.answer(f"Не удалось применить доступ: <code>{str(exc)[:500]}</code>\nПоддержка: @hawkuy")
    link = device_link(secret)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Подключить в Telegram", url=link)]])
    await message.answer(f"✅ Устройство <b>{name}</b> добавлено.\n<code>{link}</code>", reply_markup=kb)


@router.callback_query(F.data.startswith("revoke_device:"))
async def revoke_device(call: CallbackQuery):
    device_id = int(call.data.split(":", 1)[1])
    with db_connect() as con:
        device = con.execute("SELECT * FROM devices WHERE id=? AND user_id=? AND active=1", (device_id, call.from_user.id)).fetchone()
        if not device:
            return await call.answer("Устройство не найдено", show_alert=True)
        con.execute("UPDATE devices SET active=0 WHERE id=?", (device_id,))
    try:
        await asyncio.to_thread(sync_mtproxy_service)
    except Exception as exc:
        with db_connect() as con:
            con.execute("UPDATE devices SET active=1 WHERE id=?", (device_id,))
        return await call.answer(f"Ошибка отключения: {str(exc)[:120]}", show_alert=True)
    await devices_menu(call)


@router.callback_query(F.data == "buy")
async def buy(call: CallbackQuery):
    upsert_user(call)
    price = get_setting("price_rub")
    days = get_setting("subscription_days")
    rows = [[InlineKeyboardButton(text="⭐ Оплатить Stars", callback_data="pay_stars")]]
    if get_setting("xrocket_token") and get_setting("xrocket_amount"):
        rows.append([InlineKeyboardButton(text="🚀 Оплатить через xRocket", callback_data="pay_xrocket")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home")])
    await safe_edit(
        call,
        f"💳 <b>Подписка на {days} дней</b>\nЦена: <b>{price} ₽</b>\n\nВыберите способ оплаты. Количество Stars и сумма xRocket задаются администратором отдельно.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "pay_stars")
async def pay_stars(call: CallbackQuery, bot: Bot):
    upsert_user(call)
    stars = int(get_setting("stars_amount", "100"))
    days = int(get_setting("subscription_days", "30"))
    payload = f"stars:{call.from_user.id}:{int(time.time())}"
    with db_connect() as con:
        con.execute(
            "INSERT INTO payments(user_id,provider,amount,currency,status,created_at,payload) VALUES(?,?,?,?,?,?,?)",
            (call.from_user.id, "stars", str(stars), "XTR", "created", int(time.time()), payload),
        )
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"Подписка на {days} дней",
        description="Доступ к MTProto-прокси",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"Подписка на {days} дней", amount=stars)],
        provider_token="",
    )
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    with db_connect() as con:
        row = con.execute("SELECT * FROM payments WHERE payload=?", (query.invoice_payload,)).fetchone()
    await query.answer(ok=bool(row and row["status"] == "created"), error_message="Счёт устарел. Создайте новый.")


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot):
    payment = message.successful_payment
    with db_connect() as con:
        row = con.execute("SELECT * FROM payments WHERE payload=?", (payment.invoice_payload,)).fetchone()
        if not row or row["status"] == "paid":
            return
        con.execute(
            "UPDATE payments SET status='paid', external_id=? WHERE payload=?",
            (payment.telegram_payment_charge_id, payment.invoice_payload),
        )
    until = grant_subscription(message.from_user.id, int(get_setting("subscription_days", "30")))
    await message.answer(
        f"✅ Оплата получена. Подписка активна до {format_until(until)}.",
        reply_markup=main_keyboard(message.from_user.id),
    )
    try:
        await bot.send_message(ADMIN_ID, f"⭐ Оплата Stars от <code>{message.from_user.id}</code>. Подписка выдана.")
    except Exception:
        pass


async def xrocket_request(method, path, data=None):
    token = get_setting("xrocket_token")
    base = get_setting("xrocket_api", "https://pay.xrocket.tg").rstrip("/")
    headers = {"Rocket-Pay-Key": token, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.request(method, base + path, json=data, timeout=25) as resp:
            text = await resp.text()
            try:
                body = json.loads(text)
            except Exception:
                body = {"raw": text}
            if resp.status >= 400:
                raise RuntimeError(f"xRocket HTTP {resp.status}: {text[:300]}")
            return body


def find_value(obj, keys):
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]
        for value in obj.values():
            found = find_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_value(value, keys)
            if found not in (None, ""):
                return found
    return None


@router.callback_query(F.data == "pay_xrocket")
async def pay_xrocket(call: CallbackQuery):
    upsert_user(call)
    token = get_setting("xrocket_token")
    amount = get_setting("xrocket_amount")
    currency = get_setting("xrocket_currency", "USDT")
    if not token or not amount:
        return await call.answer("xRocket пока не настроен", show_alert=True)
    payload = f"xrocket:{call.from_user.id}:{int(time.time())}"
    request_data = {
        "amount": float(amount),
        "currency": currency,
        "description": f"MTProto, {get_setting('subscription_days')} дней",
        "payload": payload,
        "expiredIn": 900,
    }
    try:
        body = await xrocket_request("POST", "/tg-invoices", request_data)
        invoice_id = str(find_value(body, ("id", "invoiceId", "invoice_id")) or "")
        link = str(find_value(body, ("link", "url", "payUrl", "pay_url")) or "")
        if not invoice_id or not link:
            raise RuntimeError("xRocket не вернул id или ссылку счёта")
        with db_connect() as con:
            con.execute(
                "INSERT INTO payments(user_id,provider,external_id,amount,currency,status,created_at,payload) VALUES(?,?,?,?,?,?,?,?)",
                (call.from_user.id, "xrocket", invoice_id, amount, currency, "created", int(time.time()), payload),
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Перейти к оплате", url=link)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_xr:{invoice_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="buy")],
        ])
        await safe_edit(call, f"Счёт xRocket на <b>{amount} {currency}</b> создан. После оплаты нажмите «Проверить оплату».", kb)
    except Exception as exc:
        await call.answer("Не удалось создать счёт", show_alert=True)
        await call.message.answer(f"Ошибка xRocket: <code>{str(exc)[:500]}</code>")


@router.callback_query(F.data.startswith("check_xr:"))
async def check_xrocket(call: CallbackQuery):
    invoice_id = call.data.split(":", 1)[1]
    with db_connect() as con:
        payment = con.execute(
            "SELECT * FROM payments WHERE external_id=? AND provider='xrocket' AND user_id=?",
            (invoice_id, call.from_user.id),
        ).fetchone()
    if not payment:
        return await call.answer("Счёт не найден", show_alert=True)
    if payment["status"] == "paid":
        return await call.answer("Этот счёт уже зачислен", show_alert=True)
    try:
        body = await xrocket_request("GET", f"/tg-invoices/{invoice_id}")
        status = str(find_value(body, ("status", "state")) or "").lower()
        if status not in {"paid", "completed", "success", "successful"}:
            return await call.answer(f"Платёж ещё не получен: {status or 'ожидание'}", show_alert=True)
        with db_connect() as con:
            changed = con.execute(
                "UPDATE payments SET status='paid' WHERE id=? AND status!='paid'",
                (payment["id"],),
            ).rowcount
        if not changed:
            return await call.answer("Этот счёт уже зачислен", show_alert=True)
        until = grant_subscription(call.from_user.id, int(get_setting("subscription_days", "30")))
        await safe_edit(call, f"✅ Оплата получена. Подписка активна до {format_until(until)}.", main_keyboard(call.from_user.id))
    except Exception as exc:
        await call.answer("Ошибка проверки платежа", show_alert=True)
        if is_admin(call.from_user.id):
            await call.message.answer(f"<code>{str(exc)[:500]}</code>")


@router.message(Command("cancel"))
async def cancel(message: Message):
    with db_connect() as con:
        con.execute("UPDATE users SET awaiting_support=0 WHERE user_id=?", (message.from_user.id,))
    await message.answer("Отменено.", reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("reply"))
async def admin_reply(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        return await message.answer("Формат: <code>/reply USER_ID текст</code>")
    try:
        await bot.send_message(int(parts[1]), "🆘 <b>Ответ поддержки:</b>\n" + parts[2])
        await message.answer("Ответ отправлен.")
    except Exception as exc:
        await message.answer(f"Не удалось отправить: <code>{str(exc)[:300]}</code>")


@router.message(Command("grant"))
async def admin_grant_command(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) not in (2, 3) or not parts[1].isdigit():
        return await message.answer("Формат: <code>/grant USER_ID [дни]</code>")
    uid = int(parts[1])
    days = int(parts[2]) if len(parts) == 3 else int(get_setting("subscription_days", "30"))
    until = grant_subscription(uid, days)
    await message.answer(f"✅ Пользователю <code>{uid}</code> выдано {days} дней, до {format_until(until)}.")
    try:
        await bot.send_message(uid, f"🎁 Вам выдана подписка на {days} дней, до {format_until(until)}.")
    except Exception:
        pass


@router.message(Command("revoke"))
async def admin_revoke_command(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Формат: <code>/revoke USER_ID</code>")
    uid = int(parts[1])
    with db_connect() as con:
        con.execute("UPDATE users SET subscription_until=0 WHERE user_id=?", (uid,))
        con.execute("UPDATE devices SET active=0 WHERE user_id=? AND active=1", (uid,))
    try:
        await asyncio.to_thread(sync_mtproxy_service)
    except Exception as exc:
        await message.answer(f"Подписка отозвана, но MTProxy не перезапущен: <code>{str(exc)[:300]}</code>")
    await message.answer(f"Подписка <code>{uid}</code> отозвана, устройства отключены.")
    try:
        await bot.send_message(uid, "Ваша подписка была отключена администратором.")
    except Exception:
        pass


@router.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await safe_edit(call, "⚙️ <b>Админ-панель</b>", admin_keyboard())


@router.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    now = int(time.time())
    with db_connect() as con:
        users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        active = con.execute("SELECT COUNT(*) c FROM users WHERE subscription_until>? AND banned=0", (now,)).fetchone()["c"]
        paid = con.execute("SELECT COUNT(*) c FROM payments WHERE status='paid'").fetchone()["c"]
    await safe_edit(call, f"📊 Пользователей: <b>{users}</b>\nАктивных подписок: <b>{active}</b>\nУспешных платежей: <b>{paid}</b>", back_keyboard("admin"))


@router.callback_query(F.data == "admin_proxy")
async def admin_proxy(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    host = get_setting("server_host") or "не задан"
    port = get_setting("server_port", "443")
    await safe_edit(call, f"MTProxy-сервер: <code>{host}:{port}</code>\n\nНастроить: <code>/set_server IP_ИЛИ_ДОМЕН 443</code>\nЛимит устройств: <code>/set_device_limit 3</code>", back_keyboard("admin"))


@router.callback_query(F.data == "admin_prices")
async def admin_prices(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    text = (
        f"💰 Цена: <b>{get_setting('price_rub')} ₽</b>\n"
        f"Stars: <b>{get_setting('stars_amount')} XTR</b>\n"
        f"Срок: <b>{get_setting('subscription_days')} дней</b>\n\n"
        "Команды:\n<code>/set_price 100</code>\n<code>/set_stars 100</code>\n<code>/set_days 30</code>"
    )
    await safe_edit(call, text, back_keyboard("admin"))


@router.callback_query(F.data == "admin_xrocket")
async def admin_xrocket(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    configured = "✅ настроен" if get_setting("xrocket_token") and get_setting("xrocket_amount") else "❌ не настроен"
    text = (
        f"🚀 xRocket: {configured}\n"
        f"Сумма: <b>{get_setting('xrocket_amount') or 'не задана'} {get_setting('xrocket_currency')}</b>\n\n"
        "Команды:\n<code>/set_xrocket_token ТОКЕН</code>\n"
        "<code>/set_xrocket_amount 1.25 USDT</code>\n"
        "Токен хранится локально. Команда с токеном будет удалена ботом."
    )
    await safe_edit(call, text, back_keyboard("admin"))


@router.callback_query(F.data == "admin_grant")
async def admin_grant_hint(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await safe_edit(call, "Выдать подписку:\n<code>/grant USER_ID 30</code>", back_keyboard("admin"))


@router.callback_query(F.data == "admin_revoke")
async def admin_revoke_hint(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await safe_edit(call, "Отозвать подписку:\n<code>/revoke USER_ID</code>", back_keyboard("admin"))


@router.message(Command("set_server"))
async def set_server_command(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) not in (2, 3):
        return await message.answer("Пример: <code>/set_server 203.0.113.10 443</code>")
    host = parts[1].strip()
    port = parts[2] if len(parts) == 3 else "443"
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        return await message.answer("Некорректный порт.")
    if any(c in host for c in " /?&<>"):
        return await message.answer("Некорректный IP или домен.")
    set_setting("server_host", host)
    set_setting("server_port", port)
    try:
        if active_devices():
            await asyncio.to_thread(sync_mtproxy_service)
    except Exception as exc:
        return await message.answer(f"Адрес сохранён, но сервис не обновлён: <code>{str(exc)[:400]}</code>")
    await message.answer(f"✅ Сервер сохранён: <code>{host}:{port}</code>")


@router.message(Command("set_device_limit"))
async def set_device_limit_command(message: Message):
    if not is_admin(message.from_user.id): return
    value = message.text.partition(" ")[2].strip()
    if not value.isdigit() or not 1 <= int(value) <= 20:
        return await message.answer("Пример: <code>/set_device_limit 3</code>")
    set_setting("device_limit", value)
    await message.answer(f"✅ Лимит устройств: {value}")


@router.message(Command("set_price"))
async def set_price_command(message: Message):
    if not is_admin(message.from_user.id): return
    value = message.text.partition(" ")[2].strip()
    if not value.isdigit() or int(value) < 1:
        return await message.answer("Пример: <code>/set_price 100</code>")
    set_setting("price_rub", value)
    await message.answer("✅ Цена изменена.")


@router.message(Command("set_stars"))
async def set_stars_command(message: Message):
    if not is_admin(message.from_user.id): return
    value = message.text.partition(" ")[2].strip()
    if not value.isdigit() or int(value) < 1:
        return await message.answer("Пример: <code>/set_stars 100</code>")
    set_setting("stars_amount", value)
    await message.answer("✅ Цена в Stars изменена.")


@router.message(Command("set_days"))
async def set_days_command(message: Message):
    if not is_admin(message.from_user.id): return
    value = message.text.partition(" ")[2].strip()
    if not value.isdigit() or not 1 <= int(value) <= 3650:
        return await message.answer("Пример: <code>/set_days 30</code>")
    set_setting("subscription_days", value)
    await message.answer("✅ Срок подписки изменён.")


@router.message(Command("set_xrocket_amount"))
async def set_xrocket_amount_command(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("Пример: <code>/set_xrocket_amount 1.25 USDT</code>")
    try:
        amount = float(parts[1])
        if amount <= 0: raise ValueError
    except ValueError:
        return await message.answer("Сумма должна быть положительным числом.")
    set_setting("xrocket_amount", str(amount))
    set_setting("xrocket_currency", parts[2].upper())
    await message.answer("✅ Сумма xRocket изменена.")


@router.message(Command("set_xrocket_token"))
async def set_xrocket_token_command(message: Message):
    if not is_admin(message.from_user.id): return
    token = message.text.partition(" ")[2].strip()
    try:
        await message.delete()
    except Exception:
        pass
    if len(token) < 10:
        return await message.answer("Токен не сохранён: слишком короткое значение.")
    set_setting("xrocket_token", token)
    await message.answer("✅ Токен xRocket сохранён локально.")


@router.message()
async def other_messages(message: Message, bot: Bot):
    upsert_user(message)
    row = get_user(message.from_user.id)
    if row and row["awaiting_support"] and not is_admin(message.from_user.id):
        with db_connect() as con:
            con.execute("UPDATE users SET awaiting_support=0 WHERE user_id=?", (message.from_user.id,))
        username = f"@{message.from_user.username}" if message.from_user.username else "без username"
        await bot.send_message(
            ADMIN_ID,
            f"🆘 Обращение от {message.from_user.full_name} ({username})\nID: <code>{message.from_user.id}</code>\nОтвет: <code>/reply {message.from_user.id} текст</code>",
        )
        try:
            await message.forward(ADMIN_ID)
        except Exception:
            await bot.send_message(ADMIN_ID, message.text or "[неподдерживаемое сообщение]")
        return await message.answer("✅ Обращение передано. Ожидайте ответа.", reply_markup=main_keyboard(message.from_user.id))
    await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard(message.from_user.id))


async def main():
    if BOT_TOKEN == "ВСТАВЬТЕ_ТОКЕН_ОТ_BOTFATHER" or not BOT_TOKEN.strip():
        raise SystemExit("Укажите BOT_TOKEN в начале файла.")
    if not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        raise SystemExit("Укажите числовой ADMIN_ID в начале файла.")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    expiry_task = asyncio.create_task(expire_devices_loop())
    print("Бот запущен. Для остановки завершите процесс в панели.", flush=True)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        expiry_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
