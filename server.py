#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
☠️ BUCKSHOT ROULETTE — Telegram Mini App (сервер)

Всё в одном файле и без установки библиотек (только стандартный Python 3.8+):
  • раздаёт саму игру (index.html) и API для неё;
  • хранит столы (комнаты) и ходы ботов-соперников;
  • проверяет подпись Telegram (initData);
  • слушает Telegram-бота (приглашения, команды);
  • сам поднимает бесплатную https-ссылку через Cloudflare Tunnel.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import html
import json
import math
import os
import platform
import random
import re
import secrets
import sqlite3
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import engine as E

HERE = Path(__file__).resolve().parent
LOCK = threading.RLock()
COND = threading.Condition(LOCK)
ROOMS: dict[str, "Room"] = {}

TOKEN = ""
DEV_MODE = False
BOT_USERNAME = ""
PUBLIC_URL = ""
PORT = 8080
HOST = "127.0.0.1"
ON_RENDER = bool(os.getenv("RENDER"))   # Render сам выставляет эту переменную
BOT_PACE = 7.5          # медленный темп по умолчанию; клиент может выбрать 2.8–7.5 сек


def normalize_bot_pace(value):
    """Безопасно ограничивает клиентскую настройку темпа бота."""
    try:
        pace = float(value)
    except (TypeError, ValueError):
        return BOT_PACE
    if not math.isfinite(pace):
        return BOT_PACE
    return max(2.8, min(7.5, pace))
GUEST_LOGIN = os.getenv("GUEST_LOGIN", "1") != "0"   # вход по имени без Telegram (GUEST_LOGIN=0 — выключить)
GUEST_BASE = 5 * 10**15        # id гостей лежат выше id пользователей Telegram и не пересекаются с ними
_RANDOM_SECRET = os.urandom(32)
_GUEST_RATE: dict[str, list] = {}
ROOM_TTL = 3 * 3600     # стол без действий закрывается через 3 часа
MAX_ROOMS = 100
DB_PATH = HERE / "buckshot.sqlite3"


def db_connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS player_stats (
            user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, games INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
            rounds_won INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS match_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, room_code TEXT NOT NULL, played_at REAL NOT NULL,
            hardcore INTEGER NOT NULL, winner_id INTEGER, winner_name TEXT NOT NULL, players_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_matches_played_at ON match_history(played_at DESC);
        """)


def record_match(room: "Room"):
    if room.recorded or room.game.state != "over":
        return
    g = room.game
    winner = g.winner()
    humans = [p for p in g.players if not p.bot]
    now = time.time()
    snapshot = [{"id": p.uid, "name": p.name, "wins": p.wins, "winner": bool(winner and p is winner)} for p in humans]
    with db_connect() as con:
        con.execute("INSERT INTO match_history(room_code, played_at, hardcore, winner_id, winner_name, players_json) VALUES(?,?,?,?,?,?)",
                    (room.code, now, int(g.hardcore), winner.uid if winner and not winner.bot else None,
                     winner.name if winner else "—", json.dumps(snapshot, ensure_ascii=False)))
        for p in humans:
            won = bool(winner and p is winner)
            con.execute("""INSERT INTO player_stats(user_id,name,games,wins,losses,rounds_won,updated)
                VALUES(?,?,1,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name, games=player_stats.games+1, wins=player_stats.wins+excluded.wins,
                losses=player_stats.losses+excluded.losses, rounds_won=player_stats.rounds_won+excluded.rounds_won,
                updated=excluded.updated""", (p.uid, p.name, int(won), int(not won), p.wins, now))
    room.recorded = True


def profile_data(uid: int, name: str = "Игрок"):
    with db_connect() as con:
        row = con.execute("SELECT * FROM player_stats WHERE user_id=?", (uid,)).fetchone()
    d = dict(row) if row else {"user_id": uid, "name": clean_name(name), "games": 0, "wins": 0, "losses": 0, "rounds_won": 0}
    d["win_rate"] = round(100 * d["wins"] / d["games"]) if d["games"] else 0
    d["achievements"] = [
        {"id":"first_match", "name":"Первый матч", "desc":"Сыграть первый матч", "icon":"🎮", "done":d["games"] >= 1},
        {"id":"first_win", "name":"Первая победа", "desc":"Выиграть первый матч", "icon":"🏆", "done":d["wins"] >= 1},
        {"id":"veteran", "name":"Ветеран подвала", "desc":"Сыграть 10 матчей", "icon":"🕯️", "done":d["games"] >= 10},
        {"id":"champion", "name":"Чемпион", "desc":"Одержать 5 побед", "icon":"👑", "done":d["wins"] >= 5},
        {"id":"survivor", "name":"Несгибаемый", "desc":"Выиграть 10 раундов", "icon":"💀", "done":d["rounds_won"] >= 10},
    ]
    return d


# ════════════════════════════════════════════════════════════════════
#  КОМНАТЫ
# ════════════════════════════════════════════════════════════════════
class Room:
    def __init__(self, code: str, game: E.Game):
        self.code = code
        self.game = game
        self.ver = 1
        self.recorded = False
        self.spectators: set[int] = set()
        self.bot_pace = BOT_PACE


def touch(room: Room):
    room.ver += 1
    room.game.last_activity = time.time()
    COND.notify_all()


def new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(random.choice(alphabet) for _ in range(4))
        if code not in ROOMS:
            return code


def create_room(uid: int, name: str, with_bot: bool = False, r1items: bool = False) -> Room:
    with LOCK:
        if len(ROOMS) >= MAX_ROOMS:
            raise ValueError("Сервер переполнен, попробуйте позже")
        g = E.Game(0, uid)
        g.first_round_items = bool(r1items)
        g.add_human(uid, name, ready=True)
        if with_bot:
            g.add_bot()
        room = Room(new_code(), g)
        ROOMS[room.code] = room
        return room


def delete_room(code: str):
    ROOMS.pop(code, None)
    COND.notify_all()


# ════════════════════════════════════════════════════════════════════
#  ПРОВЕРКА ПОДПИСИ TELEGRAM
# ════════════════════════════════════════════════════════════════════
def clean_name(n: str) -> str:
    n = re.sub(r"\s+", " ", (n or "").strip())[:24]
    return n or "Игрок"


def guest_secret() -> bytes:
    base = os.getenv("GUEST_SECRET") or TOKEN
    if base:   # стабильный ключ: гости остаются «своими» после перезапуска сервера
        return hashlib.sha256(b"buckshot-guest|" + base.encode()).digest()
    return _RANDOM_SECRET


def _guest_sig(payload: str) -> str:
    return hmac.new(guest_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_guest(name: str) -> dict:
    """Создаёт гостя для игры без Telegram: личный подписанный токен вместо initData."""
    name = clean_name(name)
    uid = GUEST_BASE + secrets.randbelow(10**15)
    nb = base64.urlsafe_b64encode(name.encode("utf-8")).decode().rstrip("=")
    payload = f"{uid}.{nb}"
    return {"token": f"guest:{payload}.{_guest_sig(payload)}", "id": uid, "name": name}


def parse_guest(init: str):
    try:
        payload, _, sig = init[len("guest:"):].rpartition(".")
        if not hmac.compare_digest(_guest_sig(payload), sig):
            return None
        uid_s, nb = payload.split(".", 1)
        uid = int(uid_s)
        if uid < GUEST_BASE:
            return None
        name = base64.urlsafe_b64decode(nb + "=" * (-len(nb) % 4)).decode("utf-8")
        return {"id": uid, "name": clean_name(name)}
    except Exception:
        return None


def guest_rate_ok(ip: str) -> bool:
    """Не больше 20 новых гостей за 10 минут с одного адреса."""
    now = time.time()
    with LOCK:
        lst = [t for t in _GUEST_RATE.get(ip, []) if now - t < 600]
        if len(lst) >= 20:
            _GUEST_RATE[ip] = lst
            return False
        lst.append(now)
        _GUEST_RATE[ip] = lst
        if len(_GUEST_RATE) > 2000:
            _GUEST_RATE.clear()
        return True


def check_init_data(init: str):
    """Возвращает {'id':..., 'name':...} или None."""
    if not init:
        return None
    if init.startswith("guest:"):
        return parse_guest(init) if GUEST_LOGIN else None
    if DEV_MODE and init.startswith("dev:"):
        try:
            _, uid, name = init.split(":", 2)
            return {"id": int(uid), "name": clean_name(name)}
        except ValueError:
            return None
    if not TOKEN:
        return None
    try:
        data = dict(urllib.parse.parse_qsl(init, keep_blank_values=True))
        got = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(data.get("auth_date", "0")) > 3 * 86400:
            return None
        u = json.loads(data["user"])
        name = ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip() \
            or u.get("username") or "Игрок"
        return {"id": int(u["id"]), "name": clean_name(name)}
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════
#  СОСТОЯНИЕ ДЛЯ КЛИЕНТА
# ════════════════════════════════════════════════════════════════════
def view(room: Room, uid: int) -> dict:
    g = room.game
    me = next((i for i, p in enumerate(g.players) if p.uid == uid), -1)
    playing = g.state == "play"
    d = {
        "code": room.code, "ver": room.ver, "state": g.state, "round": g.round,
        "hardcore": g.hardcore, "host": g.host, "me": me, "turn": g.turn if playing else -1,
        "players": [{"id": p.uid, "name": p.name, "bot": p.bot, "ready": p.ready, "hp": p.hp, "max": p.max_hp,
                     "items": p.items, "cuffed": p.cuffed, "alive": p.alive, "wins": p.wins}
                    for p in g.players],
        # Начальный состав текущей зарядки объявляется всем игрокам и потому
        # остаётся доступен. Текущий остаток и лента выбывших патронов в
        # хардкоре вообще не передаются клиенту.
        "live0": g.load_live, "blank0": g.load_blank,
        "bel": None if g.hardcore else [g.bel_live, g.bel_blank],
        "sawed": g.sawed, "log": ([line for line in g.log if "стреляет" not in line and "выбрасывает патрон" not in line and "Ствол заряжен:" not in line][-4:] if g.hardcore else g.log[-30:]),
        # Снимки состояния нужны только свежим событиям; у остальных их не отправляем (экономия трафика)
        "ev": [e if (e["id"] > g.ev_seq - 8 or "st" not in e) else {k: v for k, v in e.items() if k != "st"}
               for e in g.events[-30:]],
        "evmax": g.ev_seq, "r1items": g.first_round_items,
        "winsNeeded": E.WINS_NEEDED, "maxPlayers": E.MAX_PLAYERS,
        "bot": BOT_USERNAME, "now": time.time(), "spectators": len(room.spectators),
        "winner": -1,
        "items": {k: [E.ITEMS[k][0], E.ITEMS[k][1], E.DESC[k]] for k in E.ITEM_ORDER},
    }
    if not g.hardcore:
        d.update({"tape": g.tape, "total": len(g.shells),
                  "left": len(g.shells) - g.idx if playing else 0})
    if g.state == "over":
        w = g.winner()
        d["winner"] = g.players.index(w) if w else -1
    if me >= 0 and playing:
        p = g.players[me]
        d["known"] = [{"n": pos - g.idx + 1, "pos": pos, "live": v}
                      for pos, v in sorted(p.known.items()) if pos >= g.idx]
        if g.turn == me:
            d["usable"] = {it: dict(zip(("ok", "why"), g.can_use(p, it))) for it in set(p.items)}
            d["cuff"] = [g.pidx(q) for q in g.cuff_targets(p)]
            d["steal"] = {str(g.pidx(q)): lst for q, lst in g.steal_options(p).items()}
    if playing and not g.cur.bot:
        d["afk"] = max(0, int(E.AFK_SECONDS - (time.time() - g.turn_since)))
    return d


# ════════════════════════════════════════════════════════════════════
#  ДЕЙСТВИЯ ИГРОКОВ
# ════════════════════════════════════════════════════════════════════
def do_act(room: Room, user: dict, d: dict) -> dict:
    g = room.game
    uid = user["id"]
    t = d.get("type")
    me = g.find(uid)
    alert = None

    def idx(key="to"):
        i = d.get(key)
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(g.players):
            raise ValueError("Неверная цель")
        return i

    def host_only(msg="Это может только хозяин стола 👑"):
        if uid != g.host:
            raise ValueError(msg)

    if g.state == "lobby":
        if not me and t == "leave":
            room.spectators.discard(uid)
            return {"ok": True, "left": True}
        if t in ("addbot", "rmbot") and not me:
            raise ValueError("Сначала сядьте за стол")
        if t == "addbot":
            g.add_bot()
        elif t == "rmbot":
            g.remove_bot()
        elif t == "mode":
            host_only("Режим выбирает хозяин стола 👑")
            g.hardcore = bool(d.get("on"))
            for player in g.players:
                if not player.bot:
                    player.ready = False
        elif t == "items1":
            host_only("Это настраивает хозяин стола 👑")
            g.first_round_items = bool(d.get("on"))
            for player in g.players:
                if not player.bot:
                    player.ready = False
        elif t == "ready":
            if not me:
                raise ValueError("Сначала сядьте за стол")
            me.ready = not me.ready
        elif t == "begin":
            host_only("Начать может только хозяин стола 👑")
            if len(g.players) < 2:
                raise ValueError("Нужно минимум 2 участника. Позовите друга или добавьте бота 🎭")
            unready = [p.name for p in g.players if not p.bot and not p.ready]
            if unready:
                raise ValueError("Ожидаем готовности: " + ", ".join(unready))
            g.log = []
            g.start_round()
        elif t == "leave":
            if not me:
                raise ValueError("Вас нет за столом")
            g.players.remove(me)
            humans = [p for p in g.players if not p.bot]
            if not humans:
                delete_room(room.code)
                return {"ok": True, "gone": True}
            if g.host == uid:
                g.host = humans[0].uid
        elif t == "host":
            host_only()
            p = g.players[idx()]
            if p.bot:
                raise ValueError("Бот не может быть хозяином")
            g.host = p.uid
        elif t == "kick":
            host_only()
            p = g.players[idx()]
            if p.uid == uid:
                raise ValueError("Себя выгнать нельзя")
            g.players.remove(p)
        elif t == "disband":
            host_only()
            delete_room(room.code)
            return {"ok": True, "gone": True}
        else:
            raise ValueError("Сейчас идёт набор игроков")

    elif g.state == "play":
        if t in ("next", "begin", "rematch"):
            # Повторное/запоздалое нажатие (например, двое нажали «Следующий раунд» почти одновременно)
            return {"ok": True, "alert": None}
        if not me and t == "leave":
            room.spectators.discard(uid)
            return {"ok": True, "left": True}
        if t == "afk":
            if not me:
                raise ValueError("Вы наблюдаете за игрой")
            cur = g.cur
            if cur.bot:
                raise ValueError("Бот не зависает 🙂")
            if cur is me:
                raise ValueError("Сейчас ваш ход!")
            wait = E.AFK_SECONDS - (time.time() - g.turn_since)
            if wait > 0:
                raise ValueError(f"{cur.name} ещё может ходить. Подождите {int(wait) + 1} с")
            g.kick(cur)
        else:
            if not me:
                raise ValueError("Вы наблюдаете за игрой")
            if g.cur is not me:
                raise ValueError(f"Сейчас ходит {g.cur.name}")
            g.turn_since = time.time()
            if t == "shoot":
                g.act_shoot(me, g.players[idx()])
            elif t == "item":
                tgt = g.players[idx()] if d.get("to") is not None else None
                alert = g.act_item(me, d.get("item"), tgt)
            elif t == "adr":
                tgt = g.players[idx()] if d.get("to") is not None else None
                alert = g.act_adr(me, g.players[idx("victim")], d.get("item"), tgt)
            else:
                raise ValueError("Неизвестное действие")

    elif g.state == "between":
        if not me and t == "leave":
            room.spectators.discard(uid)
            return {"ok": True, "left": True}
        if not me:
            raise ValueError("Вы наблюдаете за игрой")
        if t != "next":
            raise ValueError("Раунд окончен")
        g.start_round()

    elif g.state == "over":
        if not me and t == "leave":
            room.spectators.discard(uid)
            return {"ok": True, "left": True}
        if not me:
            raise ValueError("Вы наблюдаете за игрой")
        if t != "rematch":
            raise ValueError("Игра окончена")
        ng = E.Game(0, g.host)
        for p in g.players:
            np = E.Player(p.uid, p.name, p.bot)
            np.ready = bool(p.bot or p.uid == g.host)
            ng.players.append(np)
        ng.bot_seq = g.bot_seq
        ng.hardcore = g.hardcore
        ng.first_round_items = g.first_round_items
        # Нумерация событий продолжается: клиент помнит последний id и отбрасывал бы
        # все события новой игры, пока их номера не перерастут старые.
        ng.ev_seq = g.ev_seq
        room.game = ng
        room.recorded = False

    room.game.last_action = time.time()
    record_match(room)
    touch(room)
    return {"ok": True, "alert": alert}


def join_room(room: Room, user: dict):
    g = room.game
    if g.find(user["id"]):
        return
    if g.state == "lobby":
        room.spectators.discard(user["id"])
        g.add_human(user["id"], user["name"], ready=False)
        g.last_action = time.time()
        touch(room)
    # Если игра уже идёт, новый участник становится зрителем.
    elif g.state in ("play", "between", "over"):
        if user["id"] not in room.spectators:
            room.spectators.add(user["id"])
            touch(room)


# ════════════════════════════════════════════════════════════════════
#  ХОДЫ БОТОВ И УБОРКА
# ════════════════════════════════════════════════════════════════════
def ticker():
    while True:
        time.sleep(0.4)
        try:
            with LOCK:
                now = time.time()
                for code, room in list(ROOMS.items()):
                    g = room.game
                    if now - g.last_activity > ROOM_TTL:
                        delete_room(code)
                        continue
                    if g.state == "play" and g.cur.bot and now - g.last_action >= room.bot_pace:
                        p = g.cur
                        try:
                            E.ai_apply(g, p, E.ai_decide(g, p))
                        except ValueError:
                            opps = [q for q in g.players if q.alive and q is not p]
                            g.act_shoot(p, random.choice(opps))
                        g.last_action = time.time()
                        record_match(room)
                        touch(room)
        except Exception:
            traceback.print_exc()


# ════════════════════════════════════════════════════════════════════
#  HTTP
# ════════════════════════════════════════════════════════════════════
class Server(ThreadingHTTPServer):
    request_queue_size = 128   # по умолчанию 5: при нескольких игроках соединения могли отбрасываться
    daemon_threads = True


_INDEX = {"mtime": None, "raw": b"", "gz": b""}


def index_bytes(want_gzip: bool):
    """index.html (118 КБ) сжимается gzip один раз и кэшируется в памяти (~30 КБ по сети)."""
    f = HERE / "index.html"
    m = f.stat().st_mtime
    if _INDEX["mtime"] != m:
        raw = f.read_bytes()
        _INDEX.update(mtime=m, raw=raw, gz=gzip.compress(raw, 6))
    return (_INDEX["gz"], True) if want_gzip else (_INDEX["raw"], False)


class Handler(BaseHTTPRequestHandler):
    server_version = "Buckshot/1.0"
    timeout = 60   # «зависшие» соединения не копятся

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            if (HERE / "index.html").exists():
                gz = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
                body, zipped = index_bytes(gz)
                extra = {"Vary": "Accept-Encoding"}
                if zipped:
                    extra["Content-Encoding"] = "gzip"
                self._send(200, body, "text/html; charset=utf-8", extra)
            else:
                self._send(404, b"index.html not found", "text/plain")
        elif path == "/health":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_HEAD(self):
        # Мониторы (UptimeRobot и т.п.) часто шлют HEAD вместо GET
        path = urllib.parse.urlparse(self.path).path
        code = 200 if path in ("/", "/index.html", "/health") else 404
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20000:
                return self._json({"error": "too big"}, 413)
            body = json.loads(self.rfile.read(n) or b"{}")
            name = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
            if name == "guest":
                if not GUEST_LOGIN:
                    return self._json({"error": "msg", "msg": "Вход без Telegram отключён на этом сервере"}, 403)
                ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
                if not guest_rate_ok(ip):
                    return self._json({"error": "msg", "msg": "Слишком много входов, попробуйте через несколько минут"}, 429)
                return self._json(make_guest(str(body.get("name") or "")))
            user = check_init_data(body.get("initData", ""))
            if not user:
                return self._json({"error": "auth", "msg": "Войдите заново: откройте игру через Telegram или введите имя"}, 401)
            self._json(self.route(name, user, body))
        except ValueError as e:
            self._json({"error": "msg", "msg": str(e)})
        except Exception:
            traceback.print_exc()
            self._json({"error": "msg", "msg": "Ошибка сервера"}, 500)

    def route(self, name: str, user: dict, b: dict) -> dict:
        code = str(b.get("room") or "").upper().strip()
        if name == "profile":
            return {"profile": profile_data(user["id"], user.get("name", "Игрок"))}
        if name == "leaderboard":
            with db_connect() as con:
                rows = con.execute("SELECT name,games,wins,losses,rounds_won FROM player_stats WHERE games>0 ORDER BY wins DESC, rounds_won DESC, games ASC LIMIT 20").fetchall()
            return {"rows": [dict(r) for r in rows]}
        if name == "history":
            with db_connect() as con:
                rows = con.execute("SELECT room_code,played_at,hardcore,winner_id,winner_name,players_json FROM match_history ORDER BY played_at DESC LIMIT 100").fetchall()
            out = []
            for row in rows:
                players = json.loads(row["players_json"])
                mine = next((x for x in players if x["id"] == user["id"]), None)
                if mine:
                    out.append({"room":row["room_code"],"played_at":row["played_at"],"hardcore":bool(row["hardcore"]),
                                "winner":row["winner_name"],"won":bool(mine["winner"]),"players":[x["name"] for x in players],"rounds_won":mine["wins"]})
                if len(out) >= 15:
                    break
            return {"matches": out}
        if name == "create":
            room = create_room(user["id"], user["name"], bool(b.get("bot")), bool(b.get("r1items")))
            return {"room": room.code}
        if name == "join":
            with LOCK:
                room = ROOMS.get(code)
                if not room:
                    raise ValueError("Стол не найден или уже закрыт")
                join_room(room, user)
                return {"room": room.code}
        if name == "poll":
            since = int(b.get("since", -1))
            with COND:
                room = ROOMS.get(code)
                if not room:
                    return {"gone": True}
                # Скорость задаётся клиентом локально; принимаем её только в комнате
                # «один человек + бот», чтобы настройки одного участника не меняли темп всем.
                pace = b.get("pace")
                humans = [p for p in room.game.players if not p.bot]
                if len(humans) == 1 and humans[0].uid == user["id"] and any(p.bot for p in room.game.players):
                    room.bot_pace = normalize_bot_pace(pace)
                if room.ver <= since:
                    COND.wait_for(lambda: code not in ROOMS or ROOMS[code].ver > since, timeout=20)
                room = ROOMS.get(code)
                if not room:
                    return {"gone": True}
                return {"ver": room.ver, "state": view(room, user["id"])}
        if name == "act":
            with LOCK:
                room = ROOMS.get(code)
                if not room:
                    return {"gone": True}
                return do_act(room, user, b)
        raise ValueError("Неизвестный запрос")


# ════════════════════════════════════════════════════════════════════
#  TELEGRAM-БОТ (приглашения и команды)
# ════════════════════════════════════════════════════════════════════
def tg(method: str, _t: int = 40, **params):
    params = {k: v for k, v in params.items() if v is not None}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_t) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except Exception:
            raise RuntimeError(f"HTTP {e.code}")
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "telegram error"))
    return data["result"]


def app_button(text: str, room: str = ""):
    url = PUBLIC_URL + ("/?room=" + room if room else "/")
    return {"inline_keyboard": [[{"text": text, "web_app": {"url": url}}]]}


def send(chat_id, text, markup=None):
    try:
        tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
           disable_web_page_preview=True, reply_markup=markup)
    except Exception as e:
        print("⚠️ Не удалось отправить сообщение:", e)


WELCOME = (
    "☠️ <b>BUCKSHOT ROULETTE</b> ☠️\n"
    "<i>Сырой подвал. Тусклая лампа. Один дробовик на всех.</i>\n\n"
    "Нажмите кнопку ниже — откроется игра.\n"
    "Чтобы играть с друзьями: создайте стол и нажмите «Пригласить друзей»."
)


def handle_message(m: dict):
    text = (m.get("text") or "").strip()
    chat = m["chat"]
    frm = m.get("from")
    if not text.startswith("/") or not frm:
        return
    head, _, payload = text.partition(" ")
    cmd, _, target = head[1:].partition("@")
    if target and target.lower() != BOT_USERNAME.lower():
        return
    cmd = cmd.lower()
    private = chat.get("type") == "private"
    if not PUBLIC_URL:
        send(chat["id"], "⏳ Игра ещё запускается. Повторите через минуту.")
        return
    if cmd in ("start", "play", "help"):
        payload = payload.strip()
        if payload.startswith("room_"):
            code = payload[5:].upper()
            with LOCK:
                exists = code in ROOMS
            if exists:
                send(chat["id"], f"🕯 Вас зовут за стол <b>{html.escape(code)}</b>.\n"
                                 "<i>Нажмите кнопку, чтобы сесть.</i>",
                     app_button("🕯 Сесть за стол", code))
            else:
                send(chat["id"], "🥀 Этот стол уже закрыт. Попросите друга создать новый.",
                     app_button("☠️ Создать свой стол"))
            return
        send(chat["id"], WELCOME, app_button("🔫 Играть") if private else None)
        if not private:
            send(chat["id"], "Чтобы начать игру здесь, напишите /newgame")
    elif cmd == "newgame":
        name = clean_name(((frm.get("first_name") or "") + " " + (frm.get("last_name") or "")))
        try:
            room = create_room(frm["id"], name)
        except ValueError as e:
            send(chat["id"], str(e))
            return
        if private:
            send(chat["id"], f"🕯 Стол <b>{room.code}</b> накрыт. Открывайте игру 👇",
                 app_button("🔫 Открыть стол", room.code))
        else:
            link = f"https://t.me/{BOT_USERNAME}?start=room_{room.code}"
            send(chat["id"],
                 f"☠️ <b>{html.escape(name)}</b> накрыл стол <b>{room.code}</b>.\n"
                 "<i>Кто не боится — садитесь.</i>",
                 {"inline_keyboard": [[{"text": "🕯 Сесть за стол", "url": link}]]})


def bot_thread():
    global BOT_USERNAME
    try:
        me = tg("getMe")
    except Exception as e:
        print(f"❌ Не удалось подключиться к Telegram: {e}\n"
              "   Проверьте токен (из @BotFather) и интернет.")
        os._exit(1)
    BOT_USERNAME = me["username"]
    try:
        tg("deleteWebhook")
        tg("setMyCommands", commands=[
            {"command": "newgame", "description": "Накрыть стол (создать игру)"},
            {"command": "play", "description": "Открыть игру"},
        ])
    except Exception as e:
        print("⚠️", e)
    offset = None
    while True:
        try:
            ups = tg_updates(offset)
        except Exception as e:
            if "onflict" in str(e):
                print("⚠️ Этот бот уже запущен где-то ещё (другое окно/устройство). Закройте его.")
            time.sleep(4)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            if "message" in u:
                try:
                    handle_message(u["message"])
                except Exception:
                    traceback.print_exc()


def tg_updates(offset):
    return tg("getUpdates", _t=75, timeout=50, offset=offset, allowed_updates=["message"])


def set_menu_button():
    if not (TOKEN and PUBLIC_URL):
        return
    try:
        tg("setChatMenuButton", menu_button={
            "type": "web_app", "text": "☠️ Играть", "web_app": {"url": PUBLIC_URL + "/"}})
    except Exception as e:
        print("⚠️ Не удалось поставить кнопку меню:", e)


# ════════════════════════════════════════════════════════════════════
#  БЕСПЛАТНАЯ HTTPS-ССЫЛКА (Cloudflare Tunnel)
# ════════════════════════════════════════════════════════════════════
def find_cloudflared():
    p = shutil.which("cloudflared")
    if p:
        return p
    local = HERE / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if local.exists():
        return str(local)
    if "com.termux" in os.environ.get("PREFIX", ""):
        print("❌ В Termux выполните:  pkg install -y cloudflared   и запустите снова.")
        return None
    system, mach = platform.system(), platform.machine().lower()
    asset = None
    if system == "Windows":
        asset = "cloudflared-windows-" + ("386.exe" if mach in ("x86", "i386", "i686") else "amd64.exe")
    elif system == "Linux":
        arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
                "armv7l": "arm", "armv6l": "arm", "i386": "386", "i686": "386"}.get(mach)
        asset = f"cloudflared-linux-{arch}" if arch else None
    if not asset:
        print("❌ Установите cloudflared вручную (Mac: brew install cloudflared) и запустите снова.")
        return None
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    print(f"⬇️  Скачиваю Cloudflare Tunnel (один раз, ~30 МБ)…")
    try:
        tmp = local.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(local)
        local.chmod(0o755)
        return str(local)
    except Exception as e:
        print(f"❌ Не удалось скачать cloudflared: {e}\n   Скачайте вручную: {url}\n"
              f"   и положите файл в папку с игрой под именем {local.name}")
        return None


def announce(url: str):
    set_menu_button()
    bar = "═" * 60
    print(f"\n{bar}\n✅ ИГРА ЗАПУЩЕНА!\n"
          f"   Откройте своего бота в Telegram и нажмите кнопку «☠️ Играть» (слева от поля ввода).\n"
          f"   Адрес игры (меняется при каждом запуске): {url}\n"
          f"   Окно не закрывайте — пока оно открыто, друзья могут играть.\n{bar}\n")


def tunnel_thread():
    global PUBLIC_URL
    while True:
        cf = find_cloudflared()
        if not cf:
            time.sleep(15)
            continue
        try:
            proc = subprocess.Popen(
                [cf, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace")
        except OSError as e:
            print("❌ Не удалось запустить cloudflared:", e)
            time.sleep(15)
            continue
        for line in proc.stdout:
            m = re.search(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com", line)
            if m and m.group(0) != PUBLIC_URL:
                PUBLIC_URL = m.group(0)
                time.sleep(3)  # даём адресу «прогреться»
                announce(PUBLIC_URL)
        proc.wait()
        PUBLIC_URL = ""
        print("⚠️ Туннель оборвался, перезапускаю через 5 секунд…")
        time.sleep(5)


def keepalive_thread():
    """Бесплатный Render усыпляет сервис после 15 минут без входящих запросов.
    Раз в 10 минут обращаемся к собственному публичному адресу, чтобы не уснуть
    (иначе бот в Telegram перестанет отвечать на команды)."""
    time.sleep(60)
    while True:
        try:
            req = urllib.request.Request(PUBLIC_URL + "/health", headers={"User-Agent": "buckshot-keepalive"})
            urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:
            print("⚠️ keep-alive:", e)
        time.sleep(600)


# ════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════════════════════════════
def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    global TOKEN, DEV_MODE, PORT, PUBLIC_URL, HOST
    init_db()
    load_env()
    TOKEN = os.getenv("BOT_TOKEN", "").strip()
    DEV_MODE = os.getenv("DEV_MODE", "") == "1"
    PORT = int(os.getenv("PORT", "8080"))
    PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if not PUBLIC_URL:
        # На Render адрес сервиса подставляется автоматически
        PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    HOST = os.getenv("HOST", "").strip() or ("0.0.0.0" if ON_RENDER else "127.0.0.1")
    if not TOKEN and not DEV_MODE and ON_RENDER:
        print("❌ Не задан BOT_TOKEN. Render → ваш сервис → Environment → добавьте BOT_TOKEN "
              "(токен от @BotFather) и сохраните.")
        sys.exit(1)
    if not TOKEN and not DEV_MODE:
        print("❌ Токен не найден (файл .env пустой или его нет).\n"
              "   Удалите файл .env из папки и запустите заново — вас снова спросят токен.\n"
              "   Токен выдаёт @BotFather.")
        sys.exit(1)

    httpd = None
    # На Render порт жёстко задан платформой — другой занимать нельзя
    for p in ([PORT] if ON_RENDER else range(PORT, PORT + 10)):
        try:
            httpd = Server((HOST, p), Handler)
            PORT = p
            break
        except OSError:
            continue
    if not httpd:
        print("❌ Не удалось занять порт. Закройте другое окно с игрой и запустите снова.")
        sys.exit(1)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=ticker, daemon=True).start()
    print(f"🖥  Сервер: http://{HOST}:{PORT}")

    if TOKEN:
        threading.Thread(target=bot_thread, daemon=True).start()
    if PUBLIC_URL:
        announce(PUBLIC_URL)
        if ON_RENDER and os.getenv("KEEP_ALIVE", "1") != "0":
            threading.Thread(target=keepalive_thread, daemon=True).start()
    elif os.getenv("NO_TUNNEL") != "1" and not ON_RENDER:
        threading.Thread(target=tunnel_thread, daemon=True).start()
    else:
        print("ℹ️  Туннель отключён (NO_TUNNEL=1).")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n👋 Остановлено.")


if __name__ == "__main__":
    main()
