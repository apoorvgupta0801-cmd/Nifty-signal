"""Trading Floor v3 - Android app (Kivy). Signals only, paper-tracked.

Home: search + add symbols, filter buttons, cards with mini charts.
Tap a card: price chart, option chain / open-interest chart, bot reasoning.
"""
import json
import threading
import time
from collections import deque

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.widget import Widget

import floor

try:
    from kivy.utils import escape_markup
except Exception:  # pragma: no cover
    def escape_markup(s):
        return s.replace("&", "&amp;").replace("[", "&bl;").replace("]", "&br;")

BG = (0.06, 0.07, 0.09, 1)
PANEL = (0.09, 0.10, 0.13, 1)
GREEN = (0.12, 0.55, 0.38, 1)
RED = (0.72, 0.24, 0.27, 1)
AMBER = (0.45, 0.36, 0.12, 1)
GREY = (0.16, 0.18, 0.23, 1)
DARK = (0.35, 0.15, 0.16, 1)
MUTED = (0.62, 0.66, 0.74, 1)
BTN = (0.20, 0.23, 0.30, 1)
BTN_ON = (0.18, 0.42, 0.70, 1)

POLL_GAP = 40      # seconds of rest after a full pass over the watchlist
STEP_GAP = 1.5     # seconds between instruments (keeps data sources happy)
FILTERS = ["All", "Index", "Stocks", "Crypto", "Signals"]


def esc(x):
    return escape_markup(str(x))


def is_signal(r):
    return bool(r) and str(r.get("action", "")).startswith(
        ("BUY", "LONG", "SHORT"))


def card_color(action):
    if (action.startswith("BUY") and action.endswith("CE")) or action == "LONG":
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
    if a in ("NO DATA", "ERROR") and r.get("errors"):
        return "[b]%s[/b]  %s" % (esc(a), esc(r["errors"][0][:60]))
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


# ------------------------------------------------------------ small widgets
def mk_label(text="", **kw):
    kw.setdefault("markup", True)
    kw.setdefault("halign", "left")
    kw.setdefault("valign", "middle")
    lab = Label(text=text, **kw)
    lab.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lab


def mk_button(text, cb, width=None, color=BTN, **kw):
    b = Button(text=text, background_normal="", background_color=color,
               size_hint_y=None, height=dp(40), **kw)
    if width:
        b.size_hint_x = None
        b.width = width
    b.bind(on_release=lambda *a: cb())
    return b


class LineChart(Widget):
    """Price line with EMA lines and entry / stoploss / target levels."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.series = None
        self.plan = None
        self.bars = 0
        self.shown = []
        self.bind(pos=self.redraw, size=self.redraw)

    def set_data(self, series, plan=None, bars=0):
        self.series, self.plan, self.bars = series, plan, bars
        self.redraw()

    def redraw(self, *a):
        self.canvas.clear()
        x0, y0 = self.x + dp(4), self.y + dp(4)
        w, h = self.width - dp(8), self.height - dp(8)
        with self.canvas:
            Color(*PANEL)
            Rectangle(pos=self.pos, size=self.size)
            s = self.series
            if not s or len(s["close"]) < 2 or w <= 0 or h <= 0:
                self.shown = []
                return
            n = self.bars or len(s["close"])
            close = s["close"][-n:]
            self.shown = close
            e9 = s.get("ema9", [])[-n:] if s.get("ema9") else []
            e21 = s.get("ema21", [])[-n:] if s.get("ema21") else []
            levels = []
            if self.plan:
                levels = [self.plan["entry"], self.plan["sl"],
                          self.plan["tp"]]
            lo, hi = floor.nice_range(close, levels + list(e9) + list(e21))
            Color(1, 1, 1, 0.07)
            for i in range(1, 4):
                yy = y0 + h * i / 4.0
                Line(points=[x0, yy, x0 + w, yy], width=1)
            if len(e21) == len(close):
                Color(1.0, 0.75, 0.25, 0.9)
                Line(points=floor.line_points(e21, x0, y0, w, h, lo, hi),
                     width=1.1)
            if len(e9) == len(close):
                Color(0.35, 0.8, 1.0, 0.9)
                Line(points=floor.line_points(e9, x0, y0, w, h, lo, hi),
                     width=1.1)
            Color(1, 1, 1, 1)
            Line(points=floor.line_points(close, x0, y0, w, h, lo, hi),
                 width=1.4)
            if self.plan:
                for val, col in ((self.plan["entry"], (1, 1, 1, 0.6)),
                                 (self.plan["sl"], (0.95, 0.3, 0.32, 1)),
                                 (self.plan["tp"], (0.2, 0.8, 0.5, 1))):
                    Color(*col)
                    yy = floor.y_of(val, y0, h, lo, hi)
                    Line(points=[x0, yy, x0 + w, yy], width=1.2)


class Spark(Widget):
    """Tiny price line shown on each card."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.vals = []
        self.bind(pos=self.redraw, size=self.redraw)

    def set_vals(self, vals):
        self.vals = list(vals or [])[-60:]
        self.redraw()

    def redraw(self, *a):
        self.canvas.clear()
        if len(self.vals) < 2 or self.width <= 0 or self.height <= 0:
            return
        lo, hi = floor.nice_range(self.vals)
        pts = floor.line_points(self.vals, self.x, self.y + dp(2),
                                self.width, self.height - dp(4), lo, hi)
        with self.canvas:
            Color(1, 1, 1, 0.75)
            Line(points=pts, width=1.2)


class OIChart(Widget):
    """Open interest by strike: calls (red) and puts (green), spot marker."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.chain = None
        self.bind(pos=self.redraw, size=self.redraw)

    def set_chain(self, chain):
        self.chain = chain
        self.redraw()

    def redraw(self, *a):
        self.canvas.clear()
        x0, y0 = self.x + dp(4), self.y + dp(4)
        w, h = self.width - dp(8), self.height - dp(8)
        with self.canvas:
            Color(*PANEL)
            Rectangle(pos=self.pos, size=self.size)
            c = self.chain
            if not c or not c.get("rows") or w <= 0 or h <= 0:
                return
            rows = c["rows"]
            for (bx, by, bw, bh, side) in floor.oi_bars(rows, x0, y0, w, h):
                if side == "CE":
                    Color(0.9, 0.32, 0.34, 1)
                else:
                    Color(0.2, 0.75, 0.5, 1)
                Rectangle(pos=(bx, by), size=(max(bw, 1), max(bh, 0)))
            sx = floor.x_for_price(rows, c["spot"], x0, w)
            Color(1, 1, 1, 0.9)
            Line(points=[sx, y0, sx, y0 + h], width=1.3)


# ------------------------------------------------------------------- card
class Card(ButtonBehavior, BoxLayout):
    def __init__(self, inst, on_open, **kw):
        super().__init__(orientation="vertical", size_hint_y=None,
                         height=dp(114), padding=dp(12), spacing=dp(2), **kw)
        self.name = inst["name"]
        self.on_open = on_open
        self.head = mk_label("[b]%s[/b]" % esc(self.name), size_hint_y=None,
                             height=dp(26), font_size="17sp")
        self.sub = mk_label("Loading...", size_hint_y=None, height=dp(24),
                            font_size="14sp", color=MUTED)
        self.spark = Spark(size_hint_y=None, height=dp(34))
        for w in (self.head, self.sub, self.spark):
            self.add_widget(w)
        with self.canvas.before:
            self.bg = Color(*GREY)
            self.rect = RoundedRectangle(pos=self.pos, size=self.size,
                                         radius=[dp(12)])
        self.bind(pos=self._sync, size=self._sync)
        self.bind(on_release=lambda *a: self.on_open(self.name))

    def _sync(self, *a):
        self.rect.pos, self.rect.size = self.pos, self.size

    def show(self, r):
        price = r.get("price")
        self.head.text = "[b]%s[/b]   %s" % (
            esc(self.name), "" if price is None else price)
        self.sub.text = sub_text(r)
        self.bg.rgba = card_color(r.get("action", ""))
        s = r.get("series")
        self.spark.set_vals(s["close"] if s else [])


# ----------------------------------------------------------- detail screen
class Detail(ModalView):
    def __init__(self, app, inst, **kw):
        super().__init__(size_hint=(1, 1), auto_dismiss=False, background="",
                         background_color=BG, **kw)
        self.app, self.inst, self.name = app, inst, inst["name"]
        self.tab, self.bars = "Chart", 60
        root = BoxLayout(orientation="vertical", padding=dp(10),
                         spacing=dp(8))
        top = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        top.add_widget(mk_button("< Back", self.dismiss, width=dp(76)))
        self.title = mk_label("[b]%s[/b]" % esc(self.name), font_size="18sp")
        top.add_widget(self.title)
        top.add_widget(mk_button("Refresh", app.refresh, width=dp(84)))
        root.add_widget(top)
        self.sig = mk_label("", size_hint_y=None, height=dp(28),
                            font_size="15sp")
        root.add_widget(self.sig)
        tabs = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        for t in ("Chart", "Options", "Bots"):
            b = ToggleButton(text=t, group="tab%d" % id(self),
                             allow_no_selection=False,
                             background_normal="", background_down="",
                             background_color=BTN_ON if t == "Chart" else BTN,
                             state="down" if t == "Chart" else "normal")
            b.bind(on_release=lambda w, t=t: self.set_tab(t))
            tabs.add_widget(b)
        self.tab_buttons = tabs.children
        root.add_widget(tabs)
        self.body = BoxLayout(orientation="vertical", spacing=dp(8))
        root.add_widget(self.body)
        if inst.get("custom"):
            root.add_widget(mk_button("Remove from my list", self.remove,
                                      color=DARK))
        self.add_widget(root)
        self.render()

    def remove(self):
        self.app.remove_instrument(self.inst)
        self.dismiss()

    def set_tab(self, t):
        self.tab = t
        for b in self.tab_buttons:
            b.background_color = BTN_ON if b.text == t else BTN
        self.render()

    def set_bars(self, n):
        self.bars = n
        self.render()

    def render(self):
        try:
            self._render()
        except Exception as e:  # never crash the app from a drawing problem
            self.body.clear_widgets()
            self.body.add_widget(mk_label("Could not draw this view: %s"
                                          % esc(e)))

    def _render(self):
        r = self.app.results.get(self.name) or {}
        price = r.get("price")
        self.title.text = "[b]%s[/b]   %s" % (
            esc(self.name), "" if price is None else price)
        self.sig.text = sub_text(r) if r else "Waiting for first data..."
        self.body.clear_widgets()
        if self.tab == "Chart":
            self._chart_tab(r)
        elif self.tab == "Options":
            self._options_tab(r)
        else:
            sv = ScrollView()
            lab = Label(text=detail_text(r, self.app.stats.get(self.name)),
                        markup=True, size_hint_y=None, halign="left",
                        valign="top", font_size="14sp")
            lab.bind(width=lambda w, v: setattr(w, "text_size", (v, None)),
                     texture_size=lambda w, v: setattr(w, "height", v[1] + 20))
            sv.add_widget(lab)
            self.body.add_widget(sv)

    def _chart_tab(self, r):
        s = r.get("series")
        if not s:
            self.body.add_widget(mk_label(
                "No price history yet for this symbol.\n"
                + "\n".join(esc(e) for e in r.get("errors", []))))
            return
        row = BoxLayout(size_hint_y=None, height=dp(36), spacing=dp(6))
        for label, n in (("Last 5h", 60), ("Last 12h", 150), ("All", 0)):
            on = (self.bars == n)
            row.add_widget(mk_button(label, lambda n=n: self.set_bars(n),
                                     color=BTN_ON if on else BTN))
        self.body.add_widget(row)
        chart = LineChart()
        self.body.add_widget(chart)
        plan = r.get("plan")
        chart.set_data(s, plan, self.bars)
        close = chart.shown or s["close"]
        hi_, lo_, last = max(close), min(close), close[-1]
        txt = "High %s   Low %s   Last %s" % (
            floor.rnd(hi_), floor.rnd(lo_), floor.rnd(last))
        if s.get("live"):
            txt += "\n[color=ffd77a]Live ticks (one point per refresh); no free history feed.[/color]"
        else:
            txt += ("\n[color=ffffff]price[/color]  [color=59ccff]EMA 9[/color]"
                    "  [color=ffbf40]EMA 21[/color]")
        if plan:
            txt += ("\n[color=ffffff]entry %s[/color]  "
                    "[color=f2494f]stoploss %s[/color]  "
                    "[color=33cc80]target %s[/color]" % (
                        plan["entry"], plan["sl"], plan["tp"]))
        self.body.add_widget(mk_label(txt, size_hint_y=None, height=dp(62),
                                      font_size="13sp", color=MUTED))

    def _options_tab(self, r):
        c = r.get("chain")
        if not c:
            why = "\n".join(esc(e) for e in r.get("errors", []))
            self.body.add_widget(mk_label(
                "No option chain for this symbol right now.\n"
                "Crypto has none, and NSE sometimes refuses requests.\n" + why))
            return
        oi = OIChart(size_hint_y=0.42)
        self.body.add_widget(oi)
        oi.set_chain(c)
        self.body.add_widget(mk_label(
            "[color=e6575a]Red = call OI[/color]   "
            "[color=33bf80]Green = put OI[/color]   White line = price\n"
            "PCR %.2f   Max pain %s   ATM IV %.1f   Expiry %s\n"
            "Support (put wall) %s   Resistance (call wall) %s" % (
                c["pcr"], c["max_pain"], c["atm_iv"], esc(c["expiry"]),
                c["support"], c["resistance"]),
            size_hint_y=None, height=dp(66), font_size="12sp", color=MUTED))
        sv = ScrollView()
        grid = GridLayout(cols=5, size_hint_y=None, row_default_height=dp(26),
                          row_force_default=True)
        grid.bind(minimum_height=grid.setter("height"))

        def cell(t, **kw):
            l = mk_label(t, halign="center", font_size="13sp", **kw)
            grid.add_widget(l)

        for h in ("CE OI", "CE LTP", "STRIKE", "PE LTP", "PE OI"):
            cell("[b]%s[/b]" % h, color=MUTED)
        near = [x for x in c["rows"]
                if abs(x["strike"] - c["atm"]) <= 6 * c["step"]]
        for x in near:
            k = str(x["strike"])
            if x["strike"] == c["atm"]:
                k = "[color=ffd77a][b]%s[/b][/color]" % k
            tag = ""
            if x["strike"] == c["resistance"]:
                tag += " R"
            if x["strike"] == c["support"]:
                tag += " S"
            sgn = lambda v: "+" if v > 0 else ("-" if v < 0 else "")
            cell("[color=e6575a]%s%s[/color]" % (
                floor.fmt_oi(x["ce_oi"]), sgn(x["ce_chg"])))
            cell("%s" % x["ce_ltp"])
            cell(k + tag)
            cell("%s" % x["pe_ltp"])
            cell("[color=33bf80]%s%s[/color]" % (
                floor.fmt_oi(x["pe_oi"]), sgn(x["pe_chg"])))
        sv.add_widget(grid)
        self.body.add_widget(sv)


# -------------------------------------------------------------------- app
class FloorApp(App):
    title = "Trading Floor"

    def build(self):
        Window.clearcolor = BG
        Window.bind(on_keyboard=self.on_key)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.results, self.stats = {}, {}
        self.journal = floor.Journal(self.user_data_dir + "/journal.json")
        self.custom_path = self.user_data_dir + "/custom.json"
        self.insts = [dict(i) for i in floor.WATCHLIST]
        self.load_custom()
        self.flt, self.shown, self.detail = "All", [], None

        root = BoxLayout(orientation="vertical", padding=dp(10),
                         spacing=dp(6))
        self.title_lab = mk_label("[b]TRADING FLOOR[/b]", halign="center",
                                  size_hint_y=None, height=dp(28),
                                  font_size="18sp")
        self.record = mk_label("Paper record: no closed signals yet",
                               halign="center", size_hint_y=None,
                               height=dp(20), font_size="13sp", color=MUTED)
        root.add_widget(self.title_lab)
        root.add_widget(self.record)

        srow = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        self.search = TextInput(
            multiline=False, hint_text="Search or add: ITC, MIDCPNIFTY, SOLUSDT",
            size_hint_y=None, height=dp(44), write_tab=False,
            background_color=(0.12, 0.14, 0.18, 1),
            foreground_color=(1, 1, 1, 1), cursor_color=(1, 1, 1, 1),
            hint_text_color=(0.5, 0.54, 0.62, 1), padding=[dp(10), dp(12)])
        self.search.bind(text=self.on_search)
        srow.add_widget(self.search)
        add = mk_button("Add", self.add_symbol, width=dp(60), color=BTN_ON)
        add.height = dp(44)
        srow.add_widget(add)
        root.add_widget(srow)

        frow = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(4))
        for f in FILTERS:
            b = ToggleButton(text=f, group="flt", allow_no_selection=False,
                             background_normal="", background_down="",
                             background_color=BTN_ON if f == "All" else BTN,
                             state="down" if f == "All" else "normal",
                             font_size="13sp")
            b.bind(on_release=lambda w, f=f: self.set_filter(f))
            frow.add_widget(b)
        self.filter_buttons = frow.children
        root.add_widget(frow)

        self.msg = mk_label("", size_hint_y=None, height=dp(18),
                            font_size="12sp", color=MUTED)
        root.add_widget(self.msg)

        scroll = ScrollView()
        self.grid = GridLayout(cols=1, spacing=dp(8), size_hint_y=None)
        self.grid.bind(minimum_height=self.grid.setter("height"))
        scroll.add_widget(self.grid)
        root.add_widget(scroll)
        root.add_widget(mk_label(
            "Tap a card for charts and option chain. Signals only: you place "
            "the trades. Educational, not advice.", halign="center",
            size_hint_y=None, height=dp(34), font_size="11sp", color=MUTED))

        self.cards = {}
        for inst in self.insts:
            self.make_card(inst)
        self.rebuild()
        return root

    # ----- list handling
    def make_card(self, inst):
        self.cards[inst["name"]] = Card(inst, self.open_detail)

    def set_filter(self, f):
        self.flt = f
        for b in self.filter_buttons:
            b.background_color = BTN_ON if b.text == f else BTN
        self.rebuild()

    def rebuild(self):
        names = floor.visible_names(self.insts, self.results,
                                    self.search.text, self.flt)
        if names != self.shown:
            self.shown = names
            self.grid.clear_widgets()
            for n in names:
                self.grid.add_widget(self.cards[n])
        if not names and not self.msg.text:
            q = self.search.text.strip().upper()
            self.msg.text = ('Nothing matches. Tap Add to track "%s".' % q
                             if q else "Nothing to show here yet.")

    def on_search(self, *a):
        self.msg.text = ""
        self.rebuild()

    def load_custom(self):
        try:
            with open(self.custom_path) as f:
                for inst in json.load(f):
                    if not any(i["name"] == inst["name"] for i in self.insts):
                        self.insts.append(inst)
        except Exception:
            pass

    def save_custom(self):
        try:
            with open(self.custom_path, "w") as f:
                json.dump([i for i in self.insts if i.get("custom")], f)
        except Exception:
            pass

    def add_symbol(self):
        inst = floor.make_instrument(self.search.text)
        if inst is None:
            self.msg.text = "Use letters and numbers only, like ITC or SOLUSDT."
            return
        if any(i["name"] == inst["name"] for i in self.insts):
            self.msg.text = "Already in the list: %s" % inst["name"]
            return
        self.insts.append(inst)
        self.make_card(inst)
        self.save_custom()
        self.msg.text = "Added %s. Fetching data..." % inst["name"]
        self.rebuild()
        self.wake.set()

    def remove_instrument(self, inst):
        self.insts = [i for i in self.insts if i["name"] != inst["name"]]
        self.cards.pop(inst["name"], None)
        self.results.pop(inst["name"], None)
        self.save_custom()
        self.shown = []
        self.rebuild()

    def refresh(self):
        self.wake.set()

    # ----- detail screen
    def open_detail(self, name):
        inst = next((i for i in self.insts if i["name"] == name), None)
        if not inst or self.detail:
            return
        self.detail = Detail(self, inst)
        self.detail.bind(on_dismiss=self._closed)
        self.detail.open()

    def _closed(self, *a):
        self.detail = None

    def on_key(self, window, key, *args):
        if key == 27 and self.detail:      # Android back button
            self.detail.dismiss()
            return True
        return False

    # ----- background data loop
    def on_start(self):
        threading.Thread(target=self.poll, daemon=True).start()

    def poll(self):
        data = floor.Data()
        hists = {}
        while True:
            for inst in list(self.insts):
                n = inst["name"]
                try:
                    r = floor.run(inst, data,
                                  hists.setdefault(n, deque(maxlen=30)),
                                  self.journal)
                except Exception as e:
                    r = dict(name=n, kind=inst["kind"], action="ERROR",
                             price=None, plan=None, votes=[], notes=[],
                             events=[], errors=[floor.short(e)], total=0.0,
                             updated=time.strftime("%H:%M:%S"))
                st = self.journal.stats(n)
                al = self.journal.stats()
                Clock.schedule_once(
                    lambda dt, r=r, st=st, al=al: self.apply(r, st, al))
                time.sleep(STEP_GAP)
            self.wake.wait(POLL_GAP)
            self.wake.clear()

    def apply(self, r, st, al):
        n = r["name"]
        if not any(i["name"] == n for i in self.insts):
            return
        self.results[n] = r
        self.stats[n] = st
        card = self.cards.get(n)
        if card:
            card.show(r)
        if al["closed"]:
            self.record.text = (
                "Paper record: %d closed, %.0f%% wins, %+.1fR, %d open" % (
                    al["closed"], al["win_rate"], al["total_r"], al["open"]))
        else:
            self.record.text = ("Paper record: no closed signals yet, "
                                "%d open" % al["open"])
        self.title_lab.text = "[b]TRADING FLOOR[/b]  %s IST" % r["updated"]
        self.rebuild()
        if self.detail and self.detail.name == n:
            self.detail.render()


if __name__ == "__main__":
    FloorApp().run()
