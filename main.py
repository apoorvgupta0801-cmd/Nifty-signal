"""Nifty Signal - Android app (Kivy). Uses the signal logic in nifty_agent.py."""
import threading
import time
from datetime import datetime, timedelta, timezone

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView

import nifty_agent as agent

IST = timezone(timedelta(hours=5, minutes=30))
POLL_SECONDS = 30
SYMBOL = "NIFTY"

GREEN = (0.12, 0.66, 0.44, 1)
RED = (0.90, 0.28, 0.30, 1)
GREY = (0.23, 0.25, 0.30, 1)


def market_open():
    n = datetime.now(IST)
    return n.weekday() < 5 and (9, 15) <= (n.hour, n.minute) <= (15, 30)


class Root(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", padding=12, spacing=10, **kw)
        self.header = Label(text="NIFTY OPTION SIGNAL", size_hint_y=None,
                            height=36, color=(0.55, 0.58, 0.65, 1))
        self.add_widget(self.header)

        self.sig = Label(text="Loading...", font_size="30sp", bold=True,
                         size_hint_y=0.22, halign="center")
        with self.sig.canvas.before:
            self.sig_color = Color(*GREY)
            self.sig_rect = Rectangle(pos=self.sig.pos, size=self.sig.size)
        self.sig.bind(pos=self._sync, size=self._sync)
        self.add_widget(self.sig)

        scroll = ScrollView()
        self.details = Label(text="", markup=True, size_hint_y=None,
                             halign="left", valign="top", font_size="15sp")
        self.details.bind(
            width=lambda w, v: setattr(w, "text_size", (v, None)),
            texture_size=lambda w, v: setattr(w, "height", v[1] + 20),
        )
        scroll.add_widget(self.details)
        self.add_widget(scroll)

        self.foot = Label(text="Educational tool. Not investment advice.",
                          size_hint_y=None, height=28, font_size="11sp",
                          color=(0.55, 0.58, 0.65, 1))
        self.add_widget(self.foot)

    def _sync(self, *a):
        self.sig_rect.pos = self.sig.pos
        self.sig_rect.size = self.sig.size

    def render(self, m, d, error, updated):
        lines = []
        if not market_open():
            lines.append("[color=ffd77a]Market closed. Showing last data; "
                         "signal is not actionable.[/color]\n")
        if error:
            lines.append("[color=ffb3b5]Data error: %s (retrying)[/color]\n"
                         % error)
        if d:
            action = d["action"]
            self.sig.text = "%s\nscore %+.1f" % (action, d["score"])
            self.sig_color.rgba = (GREEN if "CE" in action else
                                   RED if "PE" in action else GREY)
            if "prem" in d:
                lines.append(
                    "[b]Entry premium[/b] %s\n[b]Premium SL[/b] %s   "
                    "[b]Target[/b] %s\n[b]Index SL[/b] %s   [b]Target[/b] %s"
                    "\n[b]Reward:Risk[/b] %.1f\n" % (
                        d["prem"], d["prem_sl"], d["prem_tp"],
                        d["idx_sl"], d["idx_tp"], d["rr"]))
        if m:
            lines.append(
                "[b]Spot[/b] %.1f   [b]ATM[/b] %s\n[b]Support[/b] %s   "
                "[b]Resistance[/b] %s\n[b]PCR[/b] %.2f   [b]Max pain[/b] %s"
                "\n[b]ATM IV[/b] %.1f   [b]Expiry[/b] %s\n" % (
                    m["spot"], m["atm"], m["support"], m["resistance"],
                    m["pcr"], m["max_pain"], m["atm_iv"], m["expiry"]))
        if d:
            lines.append("[b]Why[/b]")
            lines += ["- " + w for w in d["why"]]
        self.details.text = "\n".join(lines)
        if updated:
            self.header.text = "NIFTY OPTION SIGNAL  %s IST" % updated


class NiftyApp(App):
    title = "Nifty Signal"

    def build(self):
        Window.clearcolor = (0.06, 0.07, 0.09, 1)
        self.root_widget = Root()
        return self.root_widget

    def on_start(self):
        threading.Thread(target=self.poll, daemon=True).start()

    def poll(self):
        session = None
        last_m = last_d = None
        while True:
            error = None
            try:
                if session is None:
                    session = agent.new_session()
                raw = agent.fetch(session, SYMBOL)
                m = agent.analyse(raw, SYMBOL)
                agent.history.append((time.time(), m["spot"]))
                last_m, last_d = m, agent.decide(m)
            except Exception as e:
                session = None
                error = str(e)[:120]
            updated = datetime.now(IST).strftime("%H:%M:%S")
            Clock.schedule_once(
                lambda dt, m=last_m, d=last_d, er=error, u=updated:
                self.root_widget.render(m, d, er, u))
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    NiftyApp().run()
