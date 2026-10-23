"""Trading Floor - Android app (Kivy). Signals only, paper-tracked."""
import threading
import time
from collections import deque

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView

import floor

try:
    from kivy.utils import escape_markup
except Exception:  # pragma: no cover
    def escape_markup(s):
        return s.replace("&", "&amp;").replace("[", "&bl;").replace("]", "&br;")

GREEN = (0.12, 0.55, 0.38, 1)
RED = (0.72, 0.24, 0.27, 1)
AMBER = (0.45, 0.36, 0.12, 1)
GREY = (0.16, 0.18, 0.23, 1)
DARK = (0.35, 0.15, 0.16, 1)
MUTED = (0.62, 0.66, 0.74, 1)

POLL_GAP = 40  # seconds of rest after a full pass over the watchlist


def esc(x):
    return escape_markup(str(x))


def card_color(action):
    if action.startswith("BUY") and action.endswith("CE") or action == "LONG":
        return GREEN
    if action.startswith("BUY") or action == "SHORT":
        return RED
    if action == "MARKET CLOSED":
        return AMBER
    if action in ("NO DATA", "ERROR"):
        return DARK
    return GREY


def sub_text(r):
    a, p = r.get("action", ""), r.get("plan")
    if p:
        tag = "OPEN  " if r.get("open") else "NEW  "
        return "%s[b]%s[/b]   SL %s   TP %s   R:R %.1f" % (
            tag, esc(a), p["sl"], p["tp"], p["rr"])
    return "[b]%s[/b]" % esc(a)


def detail_text(r, stats):
    L = []
    p = r.get("plan")
    if p:
        if "prem" in p:
            L.append("[b]Option[/b] premium %s   SL %s   Target %s" % (
                p["prem"], p["prem_sl"], p["prem_tp"]))
        L.append("[b]%s level[/b] entry %s   SL %s   Target %s" % (
            "Index" if "prem" in p else "Price", p["entry"], p["sl"],
            p["tp"]))
    for e in r.get("events", []):
        L.append("[b]Update:[/b] " + esc(e))
    L.append("")
    L.append("[b]Bots[/b]  (total %+.1f)" % r.get("total", 0))
    for v in r.get("votes", []):
        head = ("%s: filter" % v["bot"] if v["bot"] == "Regime filter"
                else "%s: %+.1f" % (v["bot"], v["score"]))
        L.append(esc(head))
        for n in v["notes"]:
            L.append("   - " + esc(n))
    if r.get("notes"):
        L.append("")
        L.append("[b]Risk manager[/b]")
        for n in r["notes"]:
            L.append("   - " + esc(n))
    if r.get("errors"):
        L.append("")
        L.append("[color=ffb3b5][b]Data issues[/b][/color]")
        for n in r["errors"]:
            L.append("   - " + esc(n))
    if stats and stats["closed"]:
        L.append("")
        L.append("Paper record here: %d closed, %.0f%% wins, %+.1fR" % (
            stats["closed"], stats["win_rate"], stats["total_r"]))
    return "\n".join(L)


class Card(BoxLayout):
    def __init__(self, inst, **kw):
        super().__init__(orientation="vertical", size_hint_y=None,
                         padding=dp(12), spacing=dp(4), **kw)
        self.expanded = False
        self.name = inst["name"]
        self.head = Label(text="[b]%s[/b]" % self.name, markup=True,
                          size_hint_y=None, height=dp(26), halign="left",
                          valign="middle", font_size="17sp")
        self.sub = Label(text="Loading...", markup=True, size_hint_y=None,
                         height=dp(24), halign="left", valign="middle",
                         font_size="14sp", color=MUTED)
        self.detail = Label(text="", markup=True, size_hint_y=None, height=0,
                            opacity=0, halign="left", valign="top",
                            font_size="13sp")
        for w in (self.head, self.sub):
            w.bind(size=lambda wd, s: setattr(wd, "text_size", s))
        self.detail.bind(width=lambda w, v: setattr(w, "text_size", (v, None)),
                         texture_size=lambda *a: self._layout())
        for w in (self.head, self.sub, self.detail):
            self.add_widget(w)
        with self.canvas.before:
            self.bg = Color(*GREY)
            self.rect = RoundedRectangle(pos=self.pos, size=self.size,
                                         radius=[dp(12)])
        self.bind(pos=self._sync, size=self._sync)
        self._layout()

    def _sync(self, *a):
        self.rect.pos, self.rect.size = self.pos, self.size

    def _layout(self):
        dh = (self.detail.texture_size[1] + dp(8)) if self.expanded else 0
        self.detail.height = dh
        self.detail.opacity = 1 if self.expanded else 0
        self.height = dp(24) + self.head.height + self.sub.height + dp(8) + dh

    def on_touch_up(self, touch):
        if self.collide_point(*touch.pos) and abs(touch.y - touch.oy) < dp(10):
            self.expanded = not self.expanded
            self._layout()
        return super().on_touch_up(touch)

    def show(self, r, stats):
        price = r.get("price")
        self.head.text = "[b]%s[/b]   %s" % (
            self.name, "" if price is None else price)
        self.sub.text = sub_text(r)
        self.bg.rgba = card_color(r.get("action", ""))
        self.detail.text = detail_text(r, stats)
        self._layout()


class Root(BoxLayout):
    def __init__(self, watchlist, **kw):
        super().__init__(orientation="vertical", padding=dp(10),
                         spacing=dp(8), **kw)
        self.title = Label(text="[b]TRADING FLOOR[/b]", markup=True,
                           size_hint_y=None, height=dp(30), font_size="18sp")
        self.record = Label(text="Paper record: no closed signals yet",
                            size_hint_y=None, height=dp(22),
                            font_size="13sp", color=MUTED)
        self.add_widget(self.title)
        self.add_widget(self.record)
        scroll = ScrollView()
        self.grid = GridLayout(cols=1, spacing=dp(8), size_hint_y=None)
        self.grid.bind(minimum_height=self.grid.setter("height"))
        scroll.add_widget(self.grid)
        self.add_widget(scroll)
        self.add_widget(Label(
            text="Tap a card for details. Signals only: you place the "
                 "trades. Educational, not advice.",
            size_hint_y=None, height=dp(34), font_size="11sp", color=MUTED))
        self.cards = {}
        for inst in watchlist:
            c = Card(inst)
            self.cards[inst["name"]] = c
            self.grid.add_widget(c)

    def update(self, r, stats, allst):
        self.cards[r["name"]].show(r, stats)
        if allst["closed"]:
            self.record.text = (
                "Paper record: %d closed, %.0f%% wins, %+.1fR, %d open" % (
                    allst["closed"], allst["win_rate"], allst["total_r"],
                    allst["open"]))
        else:
            self.record.text = ("Paper record: no closed signals yet, "
                                "%d open" % allst["open"])
        self.title.text = "[b]TRADING FLOOR[/b]  %s IST" % r["updated"]


class FloorApp(App):
    title = "Trading Floor"

    def build(self):
        Window.clearcolor = (0.06, 0.07, 0.09, 1)
        self.journal = floor.Journal(self.user_data_dir + "/journal.json")
        self.root_w = Root(floor.WATCHLIST)
        return self.root_w

    def on_start(self):
        threading.Thread(target=self.poll, daemon=True).start()

    def poll(self):
        data = floor.Data()
        hists = {i["name"]: deque(maxlen=30) for i in floor.WATCHLIST}
        while True:
            for inst in floor.WATCHLIST:
                try:
                    r = floor.run(inst, data, hists[inst["name"]],
                                  self.journal)
                except Exception as e:
                    r = dict(name=inst["name"], action="ERROR", price=None,
                             plan=None, votes=[], notes=[], events=[],
                             errors=[floor.short(e)], total=0.0,
                             updated=time.strftime("%H:%M:%S"))
                st = self.journal.stats(inst["name"])
                al = self.journal.stats()
                Clock.schedule_once(
                    lambda dt, r=r, st=st, al=al: self.root_w.update(r, st, al))
                time.sleep(2)
            time.sleep(POLL_GAP)


if __name__ == "__main__":
    FloorApp().run()
