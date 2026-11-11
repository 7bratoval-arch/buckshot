# -*- coding: utf-8 -*-
"""Игровой движок Buckshot Roulette (без Telegram и без сети)."""
from __future__ import annotations

import html
import random
import time
from dataclasses import dataclass, field

ITEMS = {  # код: (эмодзи, название)
    "glass": ("🔍", "Лупа"),
    "beer":  ("🍺", "Пиво"),
    "cig":   ("🚬", "Сигареты"),
    "cuffs": ("⛓️", "Наручники"),
    "saw":   ("🪚", "Пила"),
    "med":   ("💊", "Таблетки"),
    "inv":   ("🔄", "Инвертор"),
    "phone": ("📱", "Телефон"),
    "adr":   ("💉", "Адреналин"),
}
ITEM_ORDER = list(ITEMS)
DESC = {
    "glass": "показывает текущий патрон. Результат видите только вы",
    "beer":  "выбрасывает текущий патрон (все видят, какой он был)",
    "cig":   "+1 жизнь (не выше максимума)",
    "cuffs": "выбранный игрок пропускает свой следующий ход",
    "saw":   "следующий выстрел наносит 2 урона (если патрон боевой)",
    "med":   "50% — +2 жизни, 50% — −1 жизнь (может убить!)",
    "inv":   "меняет текущий патрон на противоположный",
    "phone": "тайно показывает последний патрон, если их больше двух; в хардкоре при двух и менее сгорает впустую",
    "adr":   "украсть предмет у соперника и сразу применить (адреналин красть нельзя)",
}
BOT_NAMES = ["Дилер", "Крупье", "Механик", "Призрак", "Торговец", "Тень"]
MAX_PLAYERS = 6
LINE = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
PHONE_MOCK = [
    "📱 Ты вообще следишь за игрой? В стволе осталось не больше двух патронов — звонить некому. "
    "Телефон сгорел впустую.",
    "📱 «Абонент недоступен.» Ствол почти пуст, а ты, видимо, не считаешь патроны. "
    "Предмет потрачен зря.",
    "📱 Гудки… гудки… Считать патроны — явно не твоё. Телефон потрачен впустую.",
    "📱 На том конце тишина. Как и в стволе. Следить за игрой надо было раньше.",
]


def roman(n: int) -> str:
    return ROMAN[n] if n < len(ROMAN) else str(n)

MAX_ITEMS = 8
WINS_NEEDED = 2       # дуэль: до двух побед в раундах (максимум три раунда)
START_HP = 5          # у всех по 5 жизней в обоих форматах
AFK_SECONDS = 90



@dataclass(eq=False)
class Player:
    uid: int
    name: str
    bot: bool = False
    hp: int = 0
    max_hp: int = 0
    items: list = field(default_factory=list)
    cuffed: bool = False
    alive: bool = True
    wins: int = 0
    known: dict = field(default_factory=dict)  # абс. позиция патрона -> боевой?
    ready: bool = False
    inv_unc: int = 0   # сколько инверсий этот игрок не может «расшифровать» (исход ему неизвестен)
    inv_adj: int = 0   # точная поправка к числу боевых от инверсий, исход которых он знал (лупа/телефон)

    @property
    def hn(self) -> str:
        return html.escape(self.name)


class Game:
    def __init__(self, chat_id: int, host_id: int):
        self.chat_id = chat_id
        self.host = host_id
        self.players: list[Player] = []
        self.state = "lobby"            # lobby | play | between | over
        self.round = 0
        self.turn = 0
        self.shells: list[bool] = []
        self.idx = 0
        self.tape: list[bool] = []
        self.load_live = self.load_blank = 0
        self.bel_live = self.bel_blank = 0
        self.sawed = False
        self.log: list[str] = []
        self.last_activity = time.time()
        self.turn_since = time.time()
        self.last_action = time.time()
        self.events: list[dict] = []
        self.ev_seq = 0
        self.bot_seq = 0
        # Хардкор — основной режим по умолчанию для всех новых столов.
        self.hardcore = True
        # Формат матча фиксируется при старте: «duel» (двое) или «ffa» (трое и больше, на выбывание)
        self.mode = None
        self.loads = 0                  # сколько раз заряжали ствол в текущем раунде
        self.inv_marks: dict[int, int] = {}   # абс. позиция -> сколько раз патрон переворачивали (публично)
        self.inv_total = 0                    # всего инверсий в текущей зарядке (для зрителей)

    # ---------- утилиты ----------
    @property
    def cur(self) -> Player:
        return self.players[self.turn]

    @property
    def fmt(self) -> str:
        """Формат матча. Дуэль — двое: раунды до 2 побед (не больше 3 раундов).
        На выбывание — трое и больше: один раунд, побеждает последний выживший."""
        return self.mode or ("ffa" if len(self.players) >= 3 else "duel")

    def belief(self, p: "Player | None" = None) -> dict:
        """Что игрок вправе знать об остатке патронов в стволе.
        Остаток = объявленный состав − выстрелянное (типы известны по ленте). Инвертор меняет состав на ±1, а исход
        знает только тот, кто видел патрон (лупа/телефон): ему состав известен точно. Остальные знают, что
        боевых может быть base±1, ±2… (с шагом 2), поэтому показываем набор возможных значений."""
        rem = max(0, len(self.shells) - self.idx)
        base = self.load_live - sum(1 for t in self.tape if t) + (p.inv_adj if p else 0)
        n = p.inv_unc if p else self.inv_total
        live = sorted({base + d for d in range(-n, n + 1, 2) if 0 <= base + d <= rem})
        if not live:
            live = [min(rem, max(0, base))]
        return {"rem": rem, "live": live, "blank": sorted(rem - v for v in live)}

    def wins_needed(self) -> int:
        return WINS_NEEDED if self.fmt == "duel" else 1

    def snapshot(self) -> dict:
        """Публичное состояние стола для показа в момент события (только то, что и так видно всем).
        Клиент держит на экране прежнее состояние и применяет снимок в нужный момент анимации,
        поэтому жизни, ход и патроны меняются в правильной очередности, а не раньше выстрела."""
        s = {
            "turn": self.turn if self.state == "play" else -1,
            "round": self.round,
            "sawed": self.sawed,
            "p": [{"hp": max(0, p.hp), "max": p.max_hp, "items": list(p.items), "cuffed": p.cuffed,
                   "alive": p.alive, "wins": p.wins} for p in self.players],
        }
        if not self.hardcore:
            s.update(live0=self.load_live, blank0=self.load_blank, bel=[self.bel_live, self.bel_blank],
                     tape=list(self.tape), total=len(self.shells), left=len(self.shells) - self.idx,
                     inv=dict(self.inv_marks))
        return s

    def emit(self, kind: str, snap: bool = True, **kw) -> dict:
        """Структурированное событие для анимаций в Mini App."""
        self.ev_seq += 1
        ev = {"id": self.ev_seq, "k": kind, **kw}
        if snap:
            ev["st"] = self.snapshot()
        self.events.append(ev)
        del self.events[:-60]
        return ev

    def pidx(self, p) -> int | None:
        return self.players.index(p) if p in self.players else None

    def say(self, text: str):
        self.log.append(text)
        del self.log[:-40]

    def find(self, uid: int) -> Player | None:
        return next((p for p in self.players if p.uid == uid), None)

    def alive_players(self):
        return [p for p in self.players if p.alive]

    # ---------- лобби ----------
    def add_human(self, uid: int, name: str, ready: bool = False):
        if len(self.players) >= MAX_PLAYERS:
            raise ValueError("Стол заполнен (максимум 6 игроков)")
        p = Player(uid, name[:24] or "Игрок")
        p.ready = ready
        self.players.append(p)

    def add_bot(self):
        if len(self.players) >= MAX_PLAYERS:
            raise ValueError("Стол заполнен (максимум 6 игроков)")
        used = {p.name for p in self.players}
        free = [n for n in BOT_NAMES if n not in used] or BOT_NAMES
        self.bot_seq += 1
        p = Player(-self.bot_seq, random.choice(free), bot=True)
        p.ready = True
        self.players.append(p)

    def remove_bot(self):
        for p in reversed(self.players):
            if p.bot:
                self.players.remove(p)
                return
        raise ValueError("Ботов за столом нет")

    # ---------- раунды ----------
    def start_round(self):
        if self.mode is None:
            self.mode = self.fmt
        self.round += 1
        self.loads = 0
        for p in self.players:
            p.hp = p.max_hp = START_HP
            p.alive = True
            p.items = []
            p.cuffed = False
            p.known = {}
        self.state = "play"
        self.turn = random.randrange(len(self.players))
        if self.fmt == "ffa":
            self.say(f"🕯 На выбывание: у каждого по {START_HP}🩸. На ногах останется только один")
        else:
            self.say(f"🕯 Раунд {roman(self.round)}: у каждого по {START_HP}🩸")
        self.load()
        self.turn_since = time.time()

    def items_per_load(self) -> int:
        """Предметы раздаются сразу: по 3 при первой зарядке раунда и по 2 при каждой следующей."""
        return 3 if self.loads <= 1 else 2

    def load(self):
        self.loads += 1
        live, blank = random.randint(1, 4), random.randint(1, 4)
        self.shells = [True] * live + [False] * blank
        random.shuffle(self.shells)
        self.idx = 0
        self.tape = []
        self.inv_marks, self.inv_total = {}, 0
        for q in self.players:
            q.inv_unc = q.inv_adj = 0
        self.load_live, self.load_blank = live, blank
        self.bel_live, self.bel_blank = live, blank
        self.sawed = False
        for p in self.players:
            p.known = {}
        self.say(f"🔁 Ствол заряжен: 🔴×{live} боевых, ⚪×{blank} холостых — порядок знает только тьма")
        n = self.items_per_load()
        if n:
            for p in self.alive_players():
                for _ in range(n):
                    if len(p.items) < MAX_ITEMS:
                        p.items.append(random.choice(ITEM_ORDER))
            self.say(f"🧰 На стол выложено по {n} предм. каждому")
        # Событие после раздачи предметов (снимок уже содержит их). В хардкоре состав
        # объявляется публично в момент зарядки, но не сохраняется в ленте событий.
        if self.hardcore:
            self.emit("reload")
        else:
            self.emit("reload", live=live, blank=blank)

    def _dec(self, live: bool):
        if live:
            self.bel_live = max(0, self.bel_live - 1)
        else:
            self.bel_blank = max(0, self.bel_blank - 1)

    def eliminate(self, p: Player):
        p.alive = False
        p.hp = 0
        p.items = []
        p.cuffed = False
        self.say(f"⚰️ {p.hn} больше не встанет")
        self.emit("die", who=self.pidx(p))

    def check_round_end(self) -> bool:
        if self.state != "play":
            return True
        alive = self.alive_players()
        if len(alive) > 1:
            return False
        if alive:
            w = alive[0]
            w.wins += 1
            if self.fmt == "ffa":
                self.say(f"🏁 Победитель: {w.hn}")
            else:
                self.say(f"🏁 Раунд {roman(self.round)}: выжил {w.hn}")
            self.emit("round", snap=False, winner=self.pidx(w))
            if w.wins >= self.wins_needed():
                self.state = "over"
                return True
        # На выбывание единственный раунд: если никого не осталось, игра всё равно закончена
        self.state = "over" if self.fmt == "ffa" else "between"
        return True

    def winner(self) -> Player | None:
        return max(self.players, key=lambda p: p.wins) if self.players else None

    def advance(self):
        before = self.turn
        self._advance()
        if self.state == "play" and self.turn != before:
            self.emit("turn", to=self.turn)

    def _advance(self):
        n = len(self.players)
        i = self.turn
        for _ in range(n * 2 + 1):
            i = (i + 1) % n
            p = self.players[i]
            if not p.alive:
                continue
            if p.cuffed:
                p.cuffed = False
                self.say(f"⛓️ {p.hn} в наручниках и пропускает ход")
                continue
            self.turn = i
            self.turn_since = time.time()
            return
        alive = [k for k, p in enumerate(self.players) if p.alive]
        if alive:
            self.turn = alive[0]
            self.turn_since = time.time()

    def after_action(self):
        if self.state != "play":
            return
        if self.check_round_end():
            return
        if not self.cur.alive:
            self.advance()
        if self.idx >= len(self.shells):
            self.load()

    # ---------- проверки предметов ----------
    def cuff_targets(self, p: Player):
        return [q for q in self.players if q.alive and q is not p and not q.cuffed]

    def can_use(self, p: Player, item: str):
        if item == "cuffs" and not self.cuff_targets(p):
            return False, "Некого сковывать: все остальные уже в наручниках"
        if item == "phone" and not self.hardcore and len(self.shells) - self.idx <= 2:
            return False, "Телефон работает, только если в стволе больше 2 патронов"
        if item == "saw" and self.sawed:
            return False, "Ствол уже обрезан"
        if item == "adr" and not self.steal_options(p):
            return False, "У соперников нечего красть"
        return True, ""

    def steal_options(self, p: Player):
        res = {}
        for q in self.players:
            if q.alive and q is not p:
                lst = [i for i in dict.fromkeys(q.items)
                       if i != "adr" and self.can_use(p, i)[0]]
                if lst:
                    res[q] = lst
        return res

    # ---------- действия ----------
    def act_shoot(self, p: Player, t: Player):
        if not t.alive:
            raise ValueError("Эта цель уже выбыла")
        live = self.shells[self.idx]
        self.idx += 1
        self.tape.append(live)
        self._dec(live)
        saw, self.sawed = self.sawed, False
        dmg = 2 if (saw and live) else 1
        if live:
            t.hp -= dmg
        # Событие — после применения урона: его снимок показывает новые жизни и ленту патронов
        # ровно в момент выстрела (ход при этом ещё у стрелявшего).
        self.emit("shot", by=self.pidx(p), to=self.pidx(t), live=live, dmg=dmg if live else 0, sawed=saw)
        who = "в себя" if t is p else f"в {t.hn}"
        if live:
            self.say(f"💥 {p.hn} стреляет {who}: 🔴 ГРОМ! −{dmg}🩸" + (" (пила!)" if dmg == 2 else ""))
            if t.hp <= 0:
                self.eliminate(t)
        else:
            self.say(f"💨 {p.hn} стреляет {who}: ⚪ сухой щелчок")
        keep_turn = (t is p) and (not live)
        if keep_turn:
            self.say(f"🎲 {p.hn} получает ещё один ход")
        if not self.check_round_end() and not keep_turn:
            self.advance()
        self.after_action()

    def act_item(self, p: Player, item: str, target: Player | None = None):
        if item not in p.items:
            raise ValueError("У вас нет этого предмета")
        ok, why = self.can_use(p, item)
        if not ok:
            raise ValueError(why)
        if item == "cuffs" and (target is None or target not in self.cuff_targets(p)):
            raise ValueError("Выберите игрока, который ещё не в наручниках")
        p.items.remove(item)
        alert = self._apply(p, item, target)
        self.after_action()
        return alert

    def act_adr(self, p: Player, victim: Player, item: str, target: Player | None = None):
        if "adr" not in p.items:
            raise ValueError("У вас нет адреналина")
        if victim is p or not victim.alive or item == "adr" or item not in victim.items:
            raise ValueError("Нельзя украсть этот предмет")
        ok, why = self.can_use(p, item)
        if not ok:
            raise ValueError(why)
        if item == "cuffs" and (target is None or target not in self.cuff_targets(p)):
            raise ValueError("Выберите игрока, который ещё не в наручниках")
        p.items.remove("adr")
        victim.items.remove(item)
        self.say(f"💉 {p.hn} колет адреналин и крадёт {ITEMS[item][0]} {ITEMS[item][1]} у {victim.hn}")
        # Сначала кража (снимок: адреналин потрачен, у жертвы предмета нет), затем применение
        # украденного. Раньше порядок был обратный: предмет «применялся» до того, как его украли.
        self.emit("steal", by=self.pidx(p), victim=self.pidx(victim), item=item)
        alert = self._apply(p, item, target)
        self.after_action()
        return alert

    def kick(self, p: Player):
        self.say(f"⏰ {p.hn} застыл во тьме слишком надолго и выбывает")
        self.eliminate(p)
        self.after_action()

    def _apply(self, p: Player, item: str, target: Player | None):
        extra = {}
        if item == "beer" and self.idx < len(self.shells):
            # По правилам все видят, какой патрон выброшен. В хардкоре это показывается
            # только в анимации (в журнале результат по-прежнему не хранится).
            extra["live"] = bool(self.shells[self.idx])
        # Событие создаётся до эффекта (чтобы возможная смерть шла после него), а снимок
        # публичного состояния прикладывается уже после эффекта.
        ev = self.emit("item", snap=False, by=self.pidx(p), item=item,
                       to=self.pidx(target) if target else None, **extra)
        alert = self._apply_effect(p, item, target)
        ev["st"] = self.snapshot()
        return alert

    def _apply_effect(self, p: Player, item: str, target: Player | None):
        if item == "glass":
            s = self.shells[self.idx]
            p.known[self.idx] = s
            self.say(f"🔍 {p.hn} вглядывается в патрон")
            return f"🔍 Текущий патрон: {'🔴 БОЕВОЙ' if s else '⚪ ХОЛОСТОЙ'}"
        if item == "beer":
            s = self.shells[self.idx]
            self.idx += 1
            self.tape.append(s)
            self._dec(s)
            self.say(f"🍺 {p.hn} пьёт пиво и выбрасывает патрон: {'🔴 боевой' if s else '⚪ холостой'}")
        elif item == "cig":
            before = p.hp
            p.hp = min(p.max_hp, p.hp + 1)
            self.say(f"🚬 {p.hn} затягивается: " + ("+1🩸" if p.hp > before else "здоровье и так полное"))
        elif item == "cuffs":
            target.cuffed = True
            self.say(f"⛓️ {p.hn} надевает наручники на {target.hn}")
        elif item == "saw":
            self.sawed = True
            self.say(f"🪚 {p.hn} обрезает ствол: следующий выстрел ×2")
        elif item == "med":
            if random.random() < 0.5:
                before = p.hp
                p.hp = min(p.max_hp, p.hp + 2)
                self.say(f"💊 {p.hn} глотает просрочку: повезло! +{p.hp - before}🩸")
            else:
                p.hp -= 1
                self.say(f"💊 {p.hn} глотает просрочку: отрава! −1🩸")
                if p.hp <= 0:
                    self.eliminate(p)
        elif item == "inv":
            # Применение публично, но исход знает только тот, кто видел патрон (лупа/телефон): его знание
            # переворачивается и оценка остатка сдвигается точно. Остальные лишь узнают, что состав мог измениться.
            pre = self.shells[self.idx]
            for q in self.players:
                if self.idx in q.known:
                    q.inv_adj += -1 if pre else 1
                    q.known[self.idx] = not pre
                else:
                    q.inv_unc += 1
            self.shells[self.idx] = not pre
            self.inv_total += 1
            self.inv_marks[self.idx] = self.inv_marks.get(self.idx, 0) + 1
            self.say(f"🔄 {p.hn} инвертирует текущий патрон")
        elif item == "phone":
            rem = len(self.shells) - self.idx
            if rem <= 2:  # возможно только в хардкоре
                self.say(f"📱 {p.hn} звонит по телефону… в трубке только смех")
                return random.choice(PHONE_MOCK)
            self.say(f"📱 {p.hn} звонит по странному телефону…")
            pos = len(self.shells) - 1
            p.known[pos] = self.shells[pos]
            return (f"📱 Последний патрон (№{rem} из {rem} в стволе): "
                    f"{'🔴 БОЕВОЙ' if self.shells[pos] else '⚪ ХОЛОСТОЙ'}")
        return None


# ════════════════════════════════════════════════════════════════════
#  ИИ БОТА
# ════════════════════════════════════════════════════════════════════
def ai_decide(g: Game, p: Player):
    opps = [q for q in g.players if q.alive and q is not p]
    rem = len(g.shells) - g.idx
    known = {k: v for k, v in p.known.items() if k >= g.idx}
    cur_known = known.get(g.idx)
    unk = rem - len(known)
    kl = sum(1 for v in known.values() if v)
    if cur_known is not None:
        pl = 1.0 if cur_known else 0.0
    elif unk > 0:
        pl = min(1.0, max(0.0, (g.bel_live - kl) / unk))
    else:
        pl = 0.5
    has = lambda i: i in p.items
    idx_of = lambda q: g.players.index(q)

    # лечение
    if has("cig") and p.hp < p.max_hp:
        return ("item", "cig", None)
    if has("med") and 2 <= p.hp <= p.max_hp - 2:
        return ("item", "med", None)
    # разведка
    if cur_known is None:
        if has("glass"):
            return ("item", "glass", None)
        if has("phone") and rem > 2:
            return ("item", "phone", None)
    # адреналин
    if has("adr"):
        best, best_sc = None, 0
        for q, lst in g.steal_options(p).items():
            for it in lst:
                sc = {"glass": 10 if cur_known is None else 0,
                      "cig": 9 if p.hp < p.max_hp else 0,
                      "cuffs": 7,
                      "saw": 6 if pl >= 0.6 else 0,
                      "inv": 6 if cur_known is False else 0,
                      "beer": 4 if (cur_known is None and pl >= 0.5) else 0,
                      "phone": 3 if rem > 2 else 0,
                      "med": 2 if 2 <= p.hp <= p.max_hp - 2 else 0}.get(it, 0)
                if sc > best_sc:
                    best, best_sc = (q, it), sc
        if best:
            q, it = best
            tgt = None
            if it == "cuffs":
                tgt = idx_of(max(g.cuff_targets(p), key=lambda z: z.hp))
            return ("adr", idx_of(q), it, tgt)
    # холостой известен → инвертируем и стреляем в соперника
    if cur_known is False and has("inv"):
        return ("item", "inv", None)
    # наручники перед выстрелом в соперника
    if pl >= 0.5 and has("cuffs") and g.cuff_targets(p):
        return ("item", "cuffs", idx_of(max(g.cuff_targets(p), key=lambda z: z.hp)))
    target = min(opps, key=lambda q: (q.hp, random.random()))
    if pl >= 0.6 and has("saw") and not g.sawed and target.hp <= 2:
        return ("item", "saw", None)
    if cur_known is None and has("beer") and p.hp <= 1 and 0.35 <= pl <= 0.65:
        return ("item", "beer", None)
    if pl >= 0.5:
        return ("shoot", idx_of(target))
    if pl <= 0.0 or (pl < 0.5 and (p.hp > 1 or pl <= 0.34)):
        return ("shoot", idx_of(p))
    return ("shoot", idx_of(target))


def ai_apply(g: Game, p: Player, act):
    kind = act[0]
    if kind == "shoot":
        g.act_shoot(p, g.players[act[1]])
    elif kind == "item":
        tgt = g.players[act[2]] if act[2] is not None else None
        g.act_item(p, act[1], tgt)
    elif kind == "adr":
        tgt = g.players[act[3]] if act[3] is not None else None
        g.act_adr(p, g.players[act[1]], act[2], tgt)


