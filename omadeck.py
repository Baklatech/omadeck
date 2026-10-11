#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 dmitri sashchenko
"""omadeck — презентации прямо в терминале, в духе Omarchy.

omadeck new — собрать презентацию в мастере, omadeck edit FILE — править,
omadeck FILE — показать,
omadeck demo — посмотреть, что умеет. Цвета берутся из текущей темы Omarchy.
"""
import argparse
import bisect
import functools
import math
import os
import random
import re
import select
import shlex
import signal
import sys
import termios
import time
import tomllib
import tty
import unicodedata

__version__ = "0.1.0"

THEME_FILE = "~/.local/state/omarchy/current/theme/colors.toml"
# запасная палитра, если темы Omarchy нет (Tokyo Night)
FALLBACK = {
    "background": "#1a1b26", "foreground": "#a9b1d6", "accent": "#7aa2f7",
    "color1": "#f7768e", "color2": "#9ece6a", "color3": "#e0af68", "color4": "#7aa2f7",
    "color5": "#ad8ee6", "color6": "#449dab", "color7": "#787c99", "color15": "#c0caf5",
}
BOLD, ITALIC = 1, 2

# русская раскладка: те же физические клавиши
RU = {"й": "q", "к": "r", "р": "h", "о": "j", "л": "k", "д": "l", "т": "n",
      "з": "p", "п": "g", "П": "G"}


class DeckError(Exception):
    pass


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def mix(a, b, t):
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def ease_out(p):
    return 1 - (1 - clamp(p)) ** 3


def ease_io(p):
    p = clamp(p)
    return 3 * p * p - 2 * p ** 3


def h01(*k):
    """Детерминированный шум 0..1 по целым ключам."""
    return (hash(k) * 2654435761 >> 7 & 0xffff) / 0xffff


@functools.lru_cache(maxsize=4096)
def cw(ch):
    if unicodedata.combining(ch) or ch in "​‍️":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def width(s):
    return sum(cw(c) for c in s)


def clip(s, w):
    if width(s) <= w:
        return s
    out, n = "", 0
    for ch in s:
        if n + cw(ch) > w - 1:
            break
        out += ch
        n += cw(ch)
    return out + "…" if w > 0 else ""


# ---------------------------------------------------------------- тема

def hexrgb(s):
    s = s.strip().lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def dist(a, b):
    return math.dist(a, b)


class Theme:
    def __init__(self, path=None):
        c = dict(FALLBACK)
        try:
            with open(os.path.expanduser(path or THEME_FILE), "rb") as f:
                c.update({k: v for k, v in tomllib.load(f).items()
                          if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v)})
        except (OSError, tomllib.TOMLDecodeError):
            if path:
                raise DeckError(f"не удалось прочитать тему: {path}")
        g = lambda k, d="foreground": hexrgb(c.get(k, c[d]))
        self.bg, self.fg, self.accent = g("background"), g("foreground"), g("accent")
        self.bright = g("color15")
        self.muted = mix(self.fg, self.bg, 0.45)
        self.faint = mix(self.fg, self.bg, 0.75)
        self.hot = mix(self.accent, (255, 255, 255), 0.8)     # вспышка на кромке эффектов
        self.stops = [self.accent, self.fg if dist(self.accent, self.fg) > 60 else self.hot]
        # цвета рядов: разные цвета темы в постоянном порядке, недостающие — смеси
        ser = []
        for k in ("accent", "color3", "color5", "color2", "color1", "color6", "color4",
                  "foreground", "color7", "color0"):
            col = g(k)
            if dist(col, self.bg) > 70 and all(dist(col, o) > 60 for o in ser):
                ser.append(col)
        base = list(ser)
        for a in base:
            for b in (self.bg, (255, 255, 255)):
                if len(ser) < 6:
                    col = mix(a, b, 0.42)
                    if all(dist(col, o) > 40 for o in ser):
                        ser.append(col)
        self.series = ser

    def grad(self, v):
        """Градиент заголовка; за пределами 0..1 отражается, чтобы переливаться."""
        v %= 2
        if v > 1:
            v = 2 - v
        n = len(self.stops) - 1
        i = min(int(v * n), n - 1)
        return mix(self.stops[i], self.stops[i + 1], v * n - i)


# ---------------------------------------------------------------- экран

@functools.lru_cache(maxsize=16384)
def sgr(fg, bg, at):
    s = "\x1b[0"
    if at & BOLD:
        s += ";1"
    if at & ITALIC:
        s += ";3"
    if fg:
        s += ";38;2;%d;%d;%d" % fg
    if bg:
        s += ";48;2;%d;%d;%d" % bg
    return s + "m"


class Screen:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.rows = [[None] * w for _ in range(h)]

    def put(self, x, y, ch, fg, bg=None, at=0):
        if 0 <= y < self.h and 0 <= x < self.w:
            self.rows[y][x] = (ch, fg, bg, at)

    def text(self, x, y, txt, fg, at=0):
        for ch in txt:
            w = cw(ch)
            if not w:
                continue
            self.put(x, y, ch, fg, None, at)
            if w == 2:
                self.put(x + 1, y, "", fg, None, at)
            x += w
        return x

    def lines(self, color=True):
        out = []
        for row in self.rows:
            buf, cur = [], None
            for c in row:
                if c is None:
                    if cur is not None:
                        buf.append("\x1b[0m")
                        cur = None
                    buf.append(" ")
                    continue
                st = c[1:]
                if color and st != cur:
                    buf.append(sgr(*st))
                    cur = st
                buf.append(c[0])
            if cur is not None:
                buf.append("\x1b[0m")
            out.append("".join(buf))
        return out


class Dots:
    """Холст из точек Брайля: 2x4 точки на знакоместо."""
    BIT = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))

    def __init__(self, cols, rows):
        self.W, self.H = cols * 2, rows * 4
        self.px = {}

    def set(self, x, y, c):
        if 0 <= x < self.W and 0 <= y < self.H:
            if c is None:
                self.px.pop((x, y), None)
            else:
                self.px[(x, y)] = c

    def line(self, x0, y0, x1, y1, c):
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for i in range(n + 1):
            f = i / n
            self.set(round(x0 + (x1 - x0) * f), round(y0 + (y1 - y0) * f), c)

    def blit(self, scr, ox, oy, keep=None):
        cells = {}
        for (x, y), c in self.px.items():
            if keep and not keep(x, y):
                continue
            k = (x >> 1, y >> 2)
            e = cells.get(k)
            if e:
                e[0] |= self.BIT[y & 3][x & 1]
                e[1] = c
            else:
                cells[k] = [self.BIT[y & 3][x & 1], c]
        for (cx, cy), (b, c) in cells.items():
            scr.put(ox + cx, oy + cy, chr(0x2800 + b), c)


# ---------------------------------------------------------------- крупный шрифт
# 5x7 пикселей; в крупном кегле пиксель — два знакоместа со срезанными углами,
# как у логотипа Omarchy, в мелком — половинка знакоместа.

_FONT = """
A .###. #...# #...# ##### #...# #...# #...#
B ####. #...# #...# ####. #...# #...# ####.
C .###. #...# #.... #.... #.... #...# .###.
D ####. #...# #...# #...# #...# #...# ####.
E ##### #.... #.... ####. #.... #.... #####
F ##### #.... #.... ####. #.... #.... #....
G .###. #...# #.... #.### #...# #...# .###.
H #...# #...# #...# ##### #...# #...# #...#
I ### .#. .#. .#. .#. .#. ###
J ..### ...#. ...#. ...#. ...#. #..#. .##..
K #...# #..#. #.#.. ##... #.#.. #..#. #...#
L #.... #.... #.... #.... #.... #.... #####
M #...# ##.## #.#.# #.#.# #...# #...# #...#
N #...# ##..# #.#.# #..## #...# #...# #...#
O .###. #...# #...# #...# #...# #...# .###.
P ####. #...# #...# ####. #.... #.... #....
Q .###. #...# #...# #...# #.#.# #..#. .##.#
R ####. #...# #...# ####. #.#.. #..#. #...#
S .#### #.... #.... .###. ....# ....# ####.
T ##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..
U #...# #...# #...# #...# #...# #...# .###.
V #...# #...# #...# #...# #...# .#.#. ..#..
W #...# #...# #...# #.#.# #.#.# ##.## #...#
X #...# #...# .#.#. ..#.. .#.#. #...# #...#
Y #...# #...# .#.#. ..#.. ..#.. ..#.. ..#..
Z ##### ....# ...#. ..#.. .#... #.... #####
0 .###. #...# #..## #.#.# ##..# #...# .###.
1 ..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.
2 .###. #...# ....# ...#. ..#.. .#... #####
3 ####. ....# ....# .###. ....# ....# ####.
4 ...#. ..##. .#.#. #..#. ##### ...#. ...#.
5 ##### #.... ####. ....# ....# #...# .###.
6 .###. #.... #.... ####. #...# #...# .###.
7 ##### ....# ...#. ..#.. .#... .#... .#...
8 .###. #...# #...# .###. #...# #...# .###.
9 .###. #...# #...# .#### ....# ....# .###.
. . . . . . . #
, .. .. .. .. .. .# #.
! # # # # # . #
? .###. #...# ....# ...#. ..#.. ..... ..#..
: . . # . # . .
- .... .... .... #### .... .... ....
+ ..... ..#.. ..#.. ##### ..#.. ..#.. .....
/ ....# ....# ...#. ..#.. .#... #.... #....
% ##..# ##..# ...#. ..#.. .#... #..## #..##
( .# #. #. #. #. #. .#
) #. .# .# .# .# .# #.
' # # . . . . .
" #.# #.# ... ... ... ... ...
_ ..... ..... ..... ..... ..... ..... #####
= ..... ..... ##### ..... ##### ..... .....
* ..... #.#.# .###. ##### .###. #.#.# .....
< ...# ..#. .#.. #... .#.. ..#. ...#
> #... .#.. ..#. ...# ..#. .#.. #...
# .#.#. .#.#. ##### .#.#. ##### .#.#. .#.#.
& .##.. #..#. #.#.. .#... #.#.# #..#. .##.#
Б ##### #.... #.... ####. #...# #...# ####.
Г ##### #.... #.... #.... #.... #.... #....
Д ..##. .#.#. .#.#. .#.#. .#.#. ##### #...#
Ё .#.#. ..... ##### #.... ####. #.... #####
Ж #.#.# #.#.# .###. ..#.. .###. #.#.# #.#.#
З .###. #...# ....# ..##. ....# #...# .###.
И #...# #...# #..## #.#.# ##..# #...# #...#
Й .#.#. ..#.. #...# #..## #.#.# ##..# #...#
Л ..### .#..# .#..# .#..# .#..# .#..# #...#
П ##### #...# #...# #...# #...# #...# #...#
У #...# #...# #...# .#### ....# ....# .###.
Ф ..#.. .###. #.#.# #.#.# #.#.# .###. ..#..
Ц #..#. #..#. #..#. #..#. #..#. ##### ....#
Ч #...# #...# #...# .#### ....# ....# ....#
Ш #.#.# #.#.# #.#.# #.#.# #.#.# #.#.# #####
Щ #.#.#. #.#.#. #.#.#. #.#.#. #.#.#. ###### .....#
Ъ ##... .#... .#... .###. .#..# .#..# .###.
Ы #...# #...# #...# ##..# #.#.# #.#.# ##..#
Ь #.... #.... #.... ####. #...# #...# ####.
Э .###. #...# ....# ..### ....# #...# .###.
Ю #..#. #.#.# #.#.# ###.# #.#.# #.#.# #..#.
Я .#### #...# #...# .#### ..#.# .#..# #...#
"""
FONT = {" ": ["..."] * 7}
for _ln in _FONT.strip().splitlines():
    _k, *_rows = _ln.split()
    FONT[_k] = _rows
# кириллица, совпадающая по начертанию с латиницей, и типографские знаки
ALIAS = dict(zip("АВЕКМНОРСТХ—–«»", 'ABEKMHOPCTX--""'))


def glyph(ch):
    ch = ch.upper()
    return FONT.get(ALIAS.get(ch, ch))


def banner_px(text):
    rows = [""] * 7
    for i, ch in enumerate(text):
        g = glyph(ch)
        if g is None:
            return None
        for r in range(7):
            rows[r] += ("." if i else "") + g[r]
    return rows


def banner(text, tier):
    px = banner_px(text)
    w = len(px[0])
    f = lambda x, y: 0 <= y < 7 and 0 <= x < w and px[y][x] == "#"
    if tier == "small":
        return ["".join(" ▀▄█"[f(x, y) + 2 * f(x, y + 1)] for x in range(w))
                for y in range(0, 8, 2)]
    out = []
    for y in range(7):
        row = []
        for x in range(w):
            if not f(x, y):
                row.append("  ")
                continue
            up, down = f(x, y - 1), f(x, y + 1)
            for s in (-1, 1):       # срез там, где штрих уходит по диагонали
                free = not f(x + s, y)
                top = free and not up and (f(x + s, y + 1) or f(x - s, y - 1))
                bot = free and not down and (f(x + s, y - 1) or f(x - s, y + 1))
                row.append("▄" if top and not bot else "▀" if bot and not top else "█")
        out.append("".join(row))
    return out


def wrap_banner(text, maxw, tier, maxlines):
    scale = 2 if tier == "big" else 1
    lines = []
    for word in text.split():
        px = banner_px(word)
        if px is None or len(px[0]) * scale > maxw:
            return None
        if lines and len(banner_px(lines[-1] + " " + word)[0]) * scale <= maxw:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines if 0 < len(lines) <= maxlines else None


def title_block(text, maxw, tiers, align):
    """Заголовок → (кегль, [(x, y, символ)], ширина, высота)."""
    text = " ".join(text.split())
    for tier in tiers:
        lines = wrap_banner(text, maxw, tier, 3 if tier == "big" else 2)
        if lines:
            blocks = [banner(l, tier) for l in lines]
            break
    else:
        tier = "plain"
        blocks = [["".join(t for t, _ in l)] for l in wrap([(text, "")], maxw) if l]
    if not blocks:
        return tier, [], 0, 0
    gap = 0 if tier == "plain" else 1
    W = max(width(b[0]) for b in blocks)
    cells, y = [], 0
    for b in blocks:
        ox = (W - width(b[0])) // 2 if align == "c" else 0
        for r, row in enumerate(b):
            x = ox
            for ch in row:
                if ch != " ":
                    cells.append((x, y + r, ch))
                x += cw(ch)
        y += len(b) + gap
    return tier, cells, W, y - gap


# ---------------------------------------------------------------- эффекты заголовка
# Каждый эффект получает клетки (x, y, символ, цвет) и возвращает длительность
# и функцию кадра: время → что нарисовать.

CIPHER = "░▒▓█▄▀▚▞▙▟▛▜#%&$@01"
RAIN = "01<>[]{}/\\|=+*:;"


def bounds(cells):
    xs, ys = [c[0] for c in cells], [c[1] for c in cells]
    return min(xs), min(ys), max(xs), max(ys)


def fx_decrypt(cells, W, H, th, rnd):
    n = len(cells)
    order = sorted(range(n), key=lambda i: (cells[i][1], cells[i][0]))
    typed = [0.0] * n
    for k, i in enumerate(order):
        typed[i] = k / n * 0.6
    solved = [0.75 + rnd.random() for _ in cells]
    dark = mix(th.accent, th.bg, 0.4)

    def frame(t):
        tick = int(t * 18)
        for i, (x, y, ch, col) in enumerate(cells):
            if t < typed[i]:
                continue
            if t >= solved[i]:
                yield x, y, ch, mix(th.hot, col, (t - solved[i]) / 0.3)
            else:
                yield x, y, CIPHER[hash((i, tick)) % len(CIPHER)], dark
    return 2.1, frame


def fx_beams(cells, W, H, th, rnd):
    x0, y0, x1, y1 = bounds(cells)
    span = x1 - x0 + 12
    rows = {y: (rnd.random() * 0.5, rnd.choice((1, -1))) for y in range(y0, y1 + 1)}
    run, sweep = 0.7, 1.2

    def frame(t):
        for x, y, ch, col in cells:
            d, dr = rows[y]
            p = (t - d) / run
            pos = ((x - x0 if dr > 0 else x1 - x) + 6) / span
            if p < pos:
                continue
            c = mix(th.hot, mix(col, th.bg, 0.6), (p - pos) * span / 8)
            q = (t - sweep) / 0.6 - (x - x0) / max(1, x1 - x0) * 0.7
            if q > 0:
                c = mix(th.hot, col, q / 0.3)
            yield x, y, ch, c
        for y, (d, dr) in rows.items():
            p = (t - d) / run
            if 0 <= p <= 1:
                hx = x0 - 6 + p * span if dr > 0 else x1 + 6 - p * span
                for k, g in enumerate("▂▁▁"):
                    yield int(hx) - dr * k, y, g, th.hot
    return sweep + 1.0, frame


def fx_rain(cells, W, H, th, rnd):
    y1 = bounds(cells)[3]
    par = [(rnd.random() * 0.9 + (y1 - c[1]) * 0.04, 0.25 + rnd.random() * 0.2) for c in cells]

    def frame(t):
        for (x, y, ch, col), (d, f) in zip(cells, par):
            p = (t - d) / f
            if p < 0:
                continue
            if p >= 1:
                yield x, y, ch, mix(th.hot, col, (p - 1) * f / 0.25)
            else:
                yield x, int(y * p * p), "│", th.accent
    return max(d + f for d, f in par) + 0.25, frame


def fx_slide(cells, W, H, th, rnd):
    y0, y1 = bounds(cells)[1::2]

    def frame(t):
        for x, y, ch, col in cells:
            k = y - y0
            p = ease_out((t - k * 0.05) / 0.7)
            if p > 0:
                off = int((1 - p) * W) * (1 if k % 2 else -1)
                yield x + off, y, ch, mix(mix(col, th.bg, 0.6), col, p)
    return (y1 - y0) * 0.05 + 0.7, frame


def fx_wipe(cells, W, H, th, rnd):
    x0, y0, x1, y1 = bounds(cells)

    def frame(t):
        p = t / 0.9 * 1.3
        for x, y, ch, col in cells:
            pos = (x - x0) / max(1, x1 - x0) * 0.8 + (y - y0) / max(1, y1 - y0) * 0.2
            if p >= pos:
                yield x, y, ch, mix(th.hot, col, (p - pos) / 0.25)
    return 1.2, frame


def fx_print(cells, W, H, th, rnd):
    order = sorted(cells, key=lambda c: (c[1], c[0]))
    n = len(order)
    dur = clamp(n / 400, 0.8, 1.8)
    tail = n * 0.08 + 1

    def frame(t):
        k = min(n, int(n * t / dur))
        for i in range(k):
            x, y, ch, col = order[i]
            yield x, y, ch, mix(th.hot, col, (k - i) / tail)
        if k < n:
            yield order[k][0], order[k][1], "█", th.hot
    return dur + 0.2, frame


def fx_scatter(cells, W, H, th, rnd):
    src = [(rnd.randrange(W), rnd.randrange(H), rnd.random() * 0.4) for _ in cells]

    def frame(t):
        for (x, y, ch, col), (sx, sy, d) in zip(cells, src):
            p = ease_io((t - d) / 1.1)
            yield (round(sx + (x - sx) * p), round(sy + (y - sy) * p), ch,
                   mix(mix(col, th.bg, 0.7), col, p))
    return 1.5, frame


def fx_expand(cells, W, H, th, rnd):
    x0, y0, x1, y1 = bounds(cells)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    def frame(t):
        p = ease_out(t / 0.9)
        for x, y, ch, col in cells:
            yield round(cx + (x - cx) * p), round(cy + (y - cy) * p), ch, mix(th.hot, col, p)
    return 0.9, frame


def fx_matrix(cells, W, H, th, rnd):
    par = {x: (rnd.random() * 0.6, 40 + rnd.random() * 30) for x in {c[0] for c in cells}}

    def frame(t):
        tick = int(t * 12)
        for x, (d, v) in par.items():
            head = int((t - d) * v)
            for k in range(8):
                y = head - k
                if 0 <= y < H - 2:
                    yield (x, y, RAIN[hash((x, y, tick)) % len(RAIN)],
                           mix(th.hot if k == 0 else th.accent, th.bg, k / 8))
        for x, y, ch, col in cells:
            d, v = par[x]
            head = (t - d) * v
            if head >= y:
                yield x, y, ch, mix(th.hot, col, (head - y) / v / 0.3)
    return 0.6 + (H + 8) / 40, frame


def fx_vhs(cells, W, H, th, rnd):
    def frame(t):
        a = max(0.0, 1 - t / 1.2)          # сила помех
        tick = int(t * 14)
        for x, y, ch, col in cells:
            if h01(x, y, tick) < a * 0.25:
                continue
            off = int((h01(y, tick) - 0.5) * a * 28) if h01(y, tick, 7) < a * 0.8 else 0
            yield x + off, y, ch, mix(th.hot if off else th.muted, col, 1 - a * 0.6)
    return 1.3, frame


EFFECTS = {
    "decrypt": fx_decrypt, "beams": fx_beams, "rain": fx_rain, "slide": fx_slide,
    "wipe": fx_wipe, "print": fx_print, "scatter": fx_scatter, "expand": fx_expand,
    "matrix": fx_matrix, "vhs": fx_vhs,
}


# ---------------------------------------------------------------- текст

INLINE = re.compile(r"(\*\*.+?\*\*|\*.+?\*|`.+?`)")


def inline(text):
    """**жирный**, *курсив*, `код` → [(текст, стиль)]."""
    out = []
    for part in INLINE.split(text):
        if not part:
            continue
        if not INLINE.fullmatch(part):
            out.append((part, ""))
        elif part.startswith("**"):
            out.append((part[2:-2], "b"))
        else:
            out.append((part[1:-1], "c" if part[0] == "`" else "i"))
    return out


def wrap(runs, maxw):
    words, pend = [], False
    for txt, st in runs:
        for m in re.finditer(r"\s+|\S+", txt):
            if m.group().isspace():
                pend = True
            else:
                words.append((m.group(), st, pend))
                pend = False
    lines, cur = [[]], 0
    for w, st, sp in words:
        ww = width(w)
        if lines[-1] and cur + ww + sp > maxw:
            lines.append([])
            cur = 0
        if sp and lines[-1]:
            lines[-1].append((" ", ""))
            cur += 1
        lines[-1].append((w, st))
        cur += ww
    return lines


def styled(th, runs, base, at=0):
    out = []
    for txt, st in runs:
        if st == "b":
            out.append((txt, th.bright, at | BOLD))
        elif st == "c":
            out.append((txt, th.accent, at))
        else:
            out.append((txt, base, at | (ITALIC if st == "i" else 0)))
    return out


def body_lines(lines, maxw, th):
    """Тело слайда → строки (отступ, [(текст, цвет, атрибуты)], начало пункта) или None."""
    out, para, code = [], [], False

    def flush():
        if para:
            for ln in wrap(inline(" ".join(para)), maxw):
                out.append((0, styled(th, ln, th.fg), False))
            para.clear()

    for raw in lines:
        s = raw.strip()
        if s.startswith("```"):
            flush()
            code = not code
            continue
        if code:
            out.append((0, [("▏ ", th.faint, 0), (raw.rstrip(), th.accent, 0)], False))
            continue
        if not s:
            flush()
            if out and out[-1] is not None:
                out.append(None)
            continue
        m = re.match(r"(\s*)([-*•]|\d+[.)])\s+(.*)", raw)
        if m:
            flush()
            lvl = len(m.group(1).expandtabs(2)) // 2
            mark = m.group(2)
            pre = (("▪" if lvl == 0 else "–") if mark in "-*•" else mark) + " "
            base = th.fg if lvl == 0 else mix(th.fg, th.bg, 0.25)
            for k, ln in enumerate(wrap(inline(m.group(3)), maxw - lvl * 3 - width(pre))):
                head = (pre if k == 0 else " " * width(pre),
                        th.accent if lvl == 0 else th.muted, BOLD)
                out.append((lvl * 3, [head] + styled(th, ln, base), k == 0 and lvl == 0))
            continue
        if s.startswith(">"):
            flush()
            for ln in wrap(inline(s[1:].strip()), maxw - 2):
                out.append((0, [("┃ ", th.accent, 0)] + styled(th, ln, th.fg, ITALIC), False))
            continue
        para.append(s)
    flush()
    while out and out[-1] is None:
        out.pop()
    return out


def airy(lines, avail):
    """Если места хватает, разделяет пункты списка пустыми строками."""
    starts = sum(1 for l in lines if l and l[2])
    if starts < 2 or len(lines) + starts > avail:
        return lines
    out = []
    for l in lines:
        if l and l[2] and out and out[-1] is not None:
            out.append(None)
        out.append(l)
    return out


def reveal(scr, x, y, runs, t, th, speed=160.0):
    """Печатает строку слева направо с горячей кромкой."""
    col = 0
    for txt, c, at in runs:
        for ch in txt:
            tc = col / speed
            if t < tc:
                return
            w = cw(ch)
            if w and ch != " ":
                fg = mix(th.hot, c, (t - tc) / 0.25)
                scr.put(x + col, y, ch, fg, None, at)
                if w == 2:
                    scr.put(x + col + 1, y, "", fg, None, at)
            col += w


def draw_lines(scr, x, y, lines, t, th, ymax):
    k = 0
    for i, l in enumerate(lines):
        if y + i >= ymax:
            break
        if l:
            reveal(scr, x + l[0], y + i, l[1], t - k * 0.06, th)
            k += 1


# ---------------------------------------------------------------- графики

def num(s):
    s = s.strip().replace("_", "").replace(" ", "").rstrip("%").replace(",", ".")
    return float(s)


def fmt(v, unit=""):
    if v == int(v):
        s = f"{int(v):,}".replace(",", " ")
    else:
        s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s + unit


def nice_scale(lo, hi, n=4):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    e = 10 ** math.floor(math.log10(raw))
    step = next(m * e for m in (1, 2, 5, 10) if m * e >= raw * 0.999)
    lo2, hi2 = math.floor(lo / step) * step, math.ceil(hi / step) * step
    return lo2, hi2, [lo2 + i * step for i in range(round((hi2 - lo2) / step) + 1)]


def unpipe(line):
    """Снимает по одной крайней черте, чтобы пустые крайние ячейки не терялись."""
    line = line.strip()
    line = line[1:] if line.startswith("|") else line
    return (line[:-1] if line.endswith("|") else line).strip()


def split_row(line):
    return [c.strip() for c in unpipe(line).split("|")]


TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def parse_data(lines, opts):
    """Данные графика: таблица (ряды в столбцах) или строки «метка: значение»."""
    labels, rows, names, rest = [], [], None, []
    table = [l for l in lines if l.strip().startswith("|")]
    try:
        if table:
            cells = [split_row(l) for l in table if not TABLE_SEP.match(l)]
            names = cells[0][1:]
            for r in cells[1:]:
                labels.append(r[0])
                rows.append([num(v) for v in r[1:]])
            rest = [l.strip() for l in lines if l.strip() and not l.strip().startswith("|")]
        else:
            for l in lines:
                m = re.match(r"\s*(?:[-*]\s+)?(.+)[:=]\s*([-+\d.,;\s%_]+)$", l)
                if m:
                    labels.append(m.group(1).strip())
                    rows.append([num(v) for v in re.split(r";\s*|,\s+|\s+", m.group(2).strip()) if v])
                elif l.strip():
                    rest.append(l.strip())
    except ValueError as e:
        raise DeckError(f"не число в данных графика: {e}")
    if not rows or not any(rows):
        raise DeckError("в графике нет данных: нужны строки «метка: значение» или таблица")
    k = max(len(r) for r in rows)
    rows = [r + [0.0] * (k - len(r)) for r in rows]
    if opts.get("series"):
        names = [s.strip() for s in str(opts["series"]).split(",")]
    if names:
        names = (names + [f"ряд {i + 1}" for i in range(k)])[:k]
    return (labels, rows, names), rest


def legend(scr, x, y, h, names, th):
    """Легенда для нескольких рядов; возвращает оставшуюся область."""
    if not names or len(names) < 2:
        return y, h
    for j, name in enumerate(names):
        scr.put(x, y, "■", th.series[j % len(th.series)])
        x = scr.text(x + 2, y, name, th.fg) + 3
    return y + 2, h - 2


def chart_bar(scr, x, y, w, h, data, p, th, unit, opts):
    labels, rows, names = data
    n, k = len(labels), len(rows[0])
    y, h = legend(scr, x, y, h, names, th)
    lo, hi, ticks = nice_scale(0, float(opts.get("max", max(max(r) for r in rows))))
    gut = max(width(fmt(v, unit)) for v in ticks) + 1
    ph = h - 3                       # сверху подписи значений, снизу ось и метки
    nt = len(ticks) - 1
    if ph >= nt * 2:
        ph -= ph % nt
    px, pw = x + gut + 1, w - gut - 1
    for tv in ticks:
        yy = y + ph - round(tv / hi * ph)
        lab = fmt(tv, unit)
        scr.text(x + gut - width(lab), yy, lab, th.muted)
        if tv:
            for i in range(pw):
                scr.put(px + i, yy, "▁", th.faint)
    for i in range(pw):
        scr.put(px + i, y + ph + 1, "▔", th.muted)
    gw = pw / n
    gap = 1 if k > 1 else 0
    bw = int(clamp((gw * 0.7 - gap * (k - 1)) / k, 1, 9))
    tot = bw * k + gap * (k - 1)
    for i in range(n):
        gx = px + int(i * gw + (gw - tot) / 2)
        pi = ease_out(p * 1.5 - i / n * 0.5)
        for j in range(k):
            v = max(0.0, rows[i][j])
            full, rem = divmod(round(min(v / hi, 1) * ph * 8 * pi), 8)
            c = th.series[j % len(th.series)] if k > 1 else th.accent
            bx = gx + j * (bw + gap)
            for q in range(bw):
                for r in range(full):
                    scr.put(bx + q, y + ph - r, "█", c)
                if rem:
                    scr.put(bx + q, y + ph - full, " ▁▂▃▄▅▆▇"[rem], c)
            lab = fmt(v if pi >= 1 else round(v * pi), unit if k == 1 else "")
            if p > 0 and width(lab) <= (gw - 1 if k == 1 else bw):
                scr.text(bx + (bw - width(lab)) // 2, y + ph - full - (1 if rem else 0),
                         lab, th.fg)
        lab = clip(labels[i], max(1, int(gw) - 1))
        scr.text(px + int(i * gw + (gw - width(lab)) / 2), y + ph + 2, lab, th.muted)


def chart_hbar(scr, x, y, w, h, data, p, th, unit, opts):
    labels, rows, names = data
    n, k = len(labels), len(rows[0])
    y, h = legend(scr, x, y, h, names, th)
    hi = float(opts.get("max", max(max(r) for r in rows))) or 1
    lw = min(max(width(l) for l in labels), w // 3)
    vw = max(width(fmt(v, unit)) for r in rows for v in r) + 1
    bw = min(w - lw - 2 - vw, 90)
    step = k + 1 if n * (k + 1) - 1 <= h else k
    y += max(0, (h - (n * step - (step - k))) // 3)
    for i in range(n):
        pi = ease_out(p * 1.5 - i / n * 0.5)
        yy = y + i * step
        if yy + k > y + h:
            break
        lab = clip(labels[i], lw)
        scr.text(x + lw - width(lab), yy + (k - 1) // 2, lab, th.muted)
        for j in range(k):
            v = max(0.0, rows[i][j])
            full, rem = divmod(round(min(v / hi, 1) * bw * 8 * pi), 8)
            c = th.series[j % len(th.series)] if k > 1 else th.accent
            for q in range(full):
                scr.put(x + lw + 2 + q, yy + j, "█", c)
            if rem:
                scr.put(x + lw + 2 + full, yy + j, " ▏▎▍▌▋▊▉"[rem], c)
            if p > 0:
                scr.text(x + lw + 3 + full + (1 if rem else 0), yy + j,
                         fmt(v if pi >= 1 else round(v * pi), unit), th.fg)


def chart_line(scr, x, y, w, h, data, p, th, unit, opts):
    labels, rows, names = data
    n, k = len(labels), len(rows[0])
    y, h = legend(scr, x, y, h, names, th)
    vals = [v for r in rows for v in r]
    lo, hi, ticks = nice_scale(float(opts.get("min", min(0.0, min(vals)))),
                               float(opts.get("max", max(vals))))
    gut = max(width(fmt(v, unit)) for v in ticks) + 1
    ph, px, pw = h - 2, x + gut + 1, w - gut - 1
    DW, DH = pw * 2 - 1, ph * 4 - 1
    X = lambda i: (i / (n - 1) if n > 1 else 0.5) * DW
    Y = lambda v: clamp((hi - v) / (hi - lo)) * DH
    for tv in ticks:
        yy = y + int(Y(tv)) // 4
        lab = fmt(tv, unit)
        scr.text(x + gut - width(lab), yy, lab, th.muted)
        for i in range(pw):
            scr.put(px + i, yy, "┈", th.faint)
    dots, xmax = Dots(pw, ph), p * DW
    marks = []
    for j in range(k):
        c = th.series[j % len(th.series)] if k > 1 else th.accent
        pts = [(X(i), Y(rows[i][j])) for i in range(n)]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 >= xmax:
                break
            if x1 > xmax:
                x1, y1 = xmax, y0 + (y1 - y0) * (xmax - x0) / (x1 - x0)
            dots.line(x0, y0, x1, y1, c)
            dots.line(x0, y0 + 1, x1, y1 + 1, c)
        marks += [(px + int(a) // 2, y + int(b) // 4, c) for a, b in pts if a <= xmax and p > 0]
    dots.blit(scr, px, y)
    if n <= pw // 4:
        for mx, my, c in marks:
            scr.put(mx, my, "●", c)
    for i in range(pw):
        scr.put(px + i, y + ph, "▔", th.muted)
    end = -1
    for i in range(n):
        lab = labels[i]
        lx = int(clamp(px + int(X(i)) // 2 - width(lab) // 2, px, px + pw - width(lab)))
        if lx > end:
            scr.text(lx, y + ph + 1, lab, th.muted)
            end = lx + width(lab)


def chart_pie(scr, x, y, w, h, data, p, th, unit, opts, donut=False):
    labels, rows, _ = data
    items = [(l, max(0.0, r[0])) for l, r in zip(labels, rows)]
    cap = len(th.series)
    if len(items) > cap:             # лишние доли складываются в «прочее»
        items.sort(key=lambda it: -it[1])
        items = items[:cap - 1] + [("прочее", sum(v for _, v in items[cap - 1:]))]
    total = sum(v for _, v in items) or 1
    cum, acc = [], 0.0
    for _, v in items:
        acc += v / total
        cum.append(acc)
    vw = max(width(fmt(v, unit)) for _, v in items)
    lw = max(width(l) for l, _ in items) + vw + 11
    R = max(3, min(h * 2, w - lw - 4, 44) // 2)
    ox = x + max(0, (w - (2 * R + 4 + lw)) // 2)
    oy = y + max(0, (h - R) // 2)
    grid = {}
    for py in range(2 * R):
        for qx in range(2 * R):
            dx, dy = qx + 0.5 - R, py + 0.5 - R
            d = math.hypot(dx, dy)
            if d > R - 0.3 or (donut and d < R * 0.56):
                continue
            a = (math.atan2(dx, -dy) / math.tau) % 1       # 0 сверху, по часовой
            if a > p:
                continue
            i = min(bisect.bisect_left(cum, a), len(items) - 1)
            edge = min(a - (cum[i - 1] if i else 0), cum[i] - a)
            if len(items) > 1 and edge * math.tau * d < 0.75:   # зазор между долями
                continue
            grid[(qx, py)] = th.series[i]
    for cy in range(R):
        for cx in range(2 * R):
            top, bot = grid.get((cx, cy * 2)), grid.get((cx, cy * 2 + 1))
            if top and bot:
                scr.put(ox + cx, oy + cy, "█" if top == bot else "▀", top,
                        None if top == bot else bot)
            elif top or bot:
                scr.put(ox + cx, oy + cy, "▀" if top else "▄", top or bot)
    if donut and R >= 6:
        lab = fmt(total if p >= 1 else round(total * p), unit)
        scr.text(ox + R - width(lab) // 2, oy + R // 2, lab, th.bright, BOLD)
    lx, ly = ox + 2 * R + 4, y + max(0, (h - len(items) * 2 + 1) // 2)
    for i, (l, v) in enumerate(items):
        t = p * 2.2 - i * 0.25
        if t <= 0:
            continue
        scr.put(lx, ly + i * 2, "■", th.series[i])
        reveal(scr, lx + 2, ly + i * 2, [
            (l.ljust(lw - vw - 11 + (len(l) - width(l))), th.fg, 0),
            ("  " + fmt(v, unit).rjust(vw), th.fg, BOLD),
            (f"  {v / total * 100:4.1f}%", th.muted, 0)], t * 0.4, th)


CHARTS = {
    "bar": chart_bar, "hbar": chart_hbar, "line": chart_line, "pie": chart_pie,
    "donut": functools.partial(chart_pie, donut=True),
}


# ---------------------------------------------------------------- иконки
# Рисуются на холсте Брайля в координатах -1..1 и зависят только от времени.

class Pen:
    def __init__(self, dots, th):
        self.d, self.th = dots, th
        n = min(dots.W, dots.H)
        self.k = (n - 1) / 2 * 0.94
        self.cx, self.cy = (dots.W - 1) / 2, (dots.H - 1) / 2
        self.bold = n >= 64
        self.A, self.F, self.M, self.B, self.H = th.accent, th.fg, th.muted, th.bright, th.hot

    def xy(self, x, y):
        return self.cx + x * self.k, self.cy + y * self.k

    def line(self, x0, y0, x1, y1, c):
        ax, ay = self.xy(x0, y0)
        bx, by = self.xy(x1, y1)
        self.d.line(ax, ay, bx, by, c)
        if self.bold:
            self.d.line(ax + 1, ay, bx + 1, by, c)
            self.d.line(ax, ay + 1, bx, by + 1, c)

    def path(self, pts, c, close=False):
        pts = list(pts)
        for a, b in zip(pts, pts[1:] + (pts[:1] if close else [])):
            self.line(a[0], a[1], b[0], b[1], c)

    def trace(self, pts, f, c):
        """Ломаная, прорисованная на долю f своей длины."""
        segs = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
        rem = sum(segs) * clamp(f)
        for (a, b), L in zip(zip(pts, pts[1:]), segs):
            if rem <= 0 or not L:
                break
            k = min(1.0, rem / L)
            self.line(a[0], a[1], a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, c)
            rem -= L

    def fill(self, pts, c):
        P = [self.xy(*q) for q in pts]
        ys = [q[1] for q in P]
        for y in range(math.ceil(min(ys)), int(max(ys)) + 1):
            xs = []
            for (x0, y0), (x1, y1) in zip(P, P[1:] + P[:1]):
                if y0 <= y < y1 or y1 <= y < y0:
                    xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
            xs.sort()
            for a, b in zip(xs[::2], xs[1::2]):
                for x in range(math.ceil(a), int(b) + 1):
                    self.d.set(x, y, c)

    def box(self, x0, y0, x1, y1, c, fill=False):
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        self.fill(pts, c) if fill else self.path(pts, c, True)

    def arc(self, cx, cy, r, c, a0=0.0, a1=math.tau, ry=None, rot=0.0):
        ry = r if ry is None else ry
        n = max(8, int(max(abs(r), abs(ry)) * self.k * abs(a1 - a0) / 2) + 2)
        cr, sr = math.cos(rot), math.sin(rot)
        pts = []
        for i in range(n + 1):
            a = a0 + (a1 - a0) * i / n
            u, v = r * math.cos(a), ry * math.sin(a)
            pts.append((cx + u * cr - v * sr, cy + u * sr + v * cr))
        self.path(pts, c)

    def disc(self, cx, cy, r, c):
        X, Y = self.xy(cx, cy)
        R = r * self.k
        for y in range(int(Y - R), int(Y + R) + 2):
            for x in range(int(X - R), int(X + R) + 2):
                if (x - X) ** 2 + (y - Y) ** 2 <= R * R:
                    self.d.set(x, y, c)


def along(pts, f):
    segs = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    rem = sum(segs) * clamp(f)
    for (a, b), L in zip(zip(pts, pts[1:]), segs):
        if rem <= L:
            k = rem / L if L else 0
            return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
        rem -= L
    return pts[-1]


def ic_rocket(p, t):
    for i in range(9):
        x = h01(i, 1) * 2 - 1
        y = (h01(i, 2) * 2 + t * (0.5 + h01(i, 3))) % 2 - 1
        p.line(x, y, x, y + 0.08, p.M)
    b = math.sin(t * 2) * 0.04
    fl = 0.28 + 0.1 * math.sin(t * 23) + 0.06 * math.sin(t * 37)
    p.fill([(-0.13, 0.34 + b), (0, 0.36 + fl + b), (0.13, 0.34 + b)], p.F)
    p.fill([(-0.06, 0.34 + b), (0, 0.34 + fl * 0.5 + b), (0.06, 0.34 + b)], p.H)
    body = [(0, -0.8), (0.1, -0.66), (0.2, -0.42), (0.22, -0.1), (0.2, 0.32),
            (-0.2, 0.32), (-0.22, -0.1), (-0.2, -0.42), (-0.1, -0.66)]
    p.fill([(x, y + b) for x, y in body], p.A)
    for sg in (-1, 1):
        p.fill([(sg * 0.2, b), (sg * 0.46, 0.4 + b), (sg * 0.2, 0.3 + b)], p.F)
    p.disc(0, -0.3 + b, 0.1, None)
    p.arc(0, -0.3 + b, 0.1, p.B)


def _gear(p, cx, cy, r, n, a, c):
    pts = []
    for i in range(n * 4):
        rr = r if i % 4 < 2 else r * 0.78
        ang = a + (i - 0.5) * math.tau / (n * 4)
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    p.path(pts, c, True)
    p.arc(cx, cy, r * 0.35, c)


def ic_gear(p, t):
    _gear(p, -0.25, -0.15, 0.6, 10, t * 0.8, p.A)
    _gear(p, 0.5, 0.47, 0.38, 6, -t * 0.8 * 10 / 6 + 0.3, p.F)


def ic_globe(p, t):
    R = 0.82
    for f in (-0.55, 0, 0.55):
        p.line(-R * math.cos(f), R * math.sin(f), R * math.cos(f), R * math.sin(f), p.M)
    for k in range(6):
        lam = (t * 0.6 + k * math.pi / 6) % math.pi - math.pi / 2
        p.arc(0, 0, R * math.sin(lam), p.F, -math.pi / 2, math.pi / 2, ry=R)
    p.arc(0, 0, R, p.A)


def ic_heart(p, t):
    ph = t * 1.1 % 1
    k = 0.05 * (1 + 0.16 * max(0.0, math.sin(ph * math.tau * 2)) * math.exp(-ph * 4))
    pts = []
    for i in range(48):
        a = i * math.tau / 48
        pts.append((k * 16 * math.sin(a) ** 3,
                    -k * (13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a)
                          - math.cos(4 * a)) - 0.08))
    p.fill(pts, p.F)
    p.path(pts, p.B, True)


def ic_clock(p, t):
    p.arc(0, 0, 0.85, p.A)
    for i in range(12):
        a = i * math.tau / 12
        r0 = 0.64 if i % 3 == 0 else 0.74
        p.line(r0 * math.cos(a), r0 * math.sin(a), 0.8 * math.cos(a), 0.8 * math.sin(a), p.M)
    m = t * 0.9
    p.line(0, 0, 0.4 * math.sin(m / 12), -0.4 * math.cos(m / 12), p.B)
    p.line(0, 0, 0.62 * math.sin(m), -0.62 * math.cos(m), p.F)
    p.disc(0, 0, 0.05, p.A)


def ic_bulb(p, t):
    g = 0.5 + 0.5 * math.sin(t * 2.5)
    for i in range(9):
        a = math.pi + i * math.pi / 8
        r1 = 0.56 + 0.18 * g * (0.5 + 0.5 * h01(i))
        p.line(0.5 * math.cos(a), -0.2 + 0.5 * math.sin(a),
               r1 * math.cos(a), -0.2 + r1 * math.sin(a), p.F)
    p.arc(0, -0.2, 0.36, p.A, math.tau / 3, math.tau + math.pi / 3)
    for sg in (-1, 1):
        p.line(sg * 0.18, 0.11, sg * 0.15, 0.4, p.A)
    for y, hw in ((0.4, 0.15), (0.5, 0.15), (0.6, 0.09)):
        p.line(-hw, y, hw, y, p.A)
    p.path([(-0.08, 0.38), (-0.11, -0.18), (0, -0.04), (0.11, -0.18), (0.08, 0.38)],
           p.H if g > 0.35 else p.M)


def ic_server(p, t):
    for i in range(3):
        y = -0.72 + i * 0.5
        p.box(-0.8, y, 0.8, y + 0.4, p.A)
        for j in range(3):
            if h01(i, j, int(t * (3 + j))) > 0.4:
                p.disc(-0.62 + j * 0.16, y + 0.2, 0.045, p.F if j else p.B)
        p.line(0.1, y + 0.14, 0.62, y + 0.14, p.M)
        p.line(0.1, y + 0.26, 0.62, y + 0.26, p.M)


def ic_database(p, t):
    rx, ry = 0.62, 0.2
    p.arc(0, -0.6, rx, p.A, ry=ry)
    for y in (-0.2, 0.2, 0.6):
        p.arc(0, y, rx, p.A, 0, math.pi, ry=ry)
    p.line(-rx, -0.6, -rx, 0.6, p.A)
    p.line(rx, -0.6, rx, 0.6, p.A)
    p.arc(0, -0.6 + (t * 0.5 % 1) * 1.2, rx * 0.97, p.F, 0, math.pi, ry=ry)


def ic_cloud(p, t):
    dx = math.sin(t * 0.6) * 0.07
    cs = [(-0.45, -0.1, 0.25), (-0.12, -0.32, 0.36), (0.32, -0.18, 0.3), (0.55, -0.05, 0.2)]
    for i, (cx, cy, r) in enumerate(cs):
        for s in range(72):
            a = s * math.tau / 72
            x, y = cx + r * math.cos(a), cy + r * math.sin(a)
            if all(math.hypot(x - ox, y - oy) >= orr - 0.01
                   for j, (ox, oy, orr) in enumerate(cs) if j != i):
                p.line(x + dx, y, x + dx, y, p.A)
    p.line(-0.45 + dx, 0.15, 0.55 + dx, 0.15, p.A)
    for i in range(6):
        ph = (t * 1.3 + h01(i, 4)) % 1
        x, y = -0.42 + i * 0.18 + dx, 0.25 + ph * 0.55
        p.line(x, y, x - 0.04, y + 0.12, p.F)


def ic_lock(p, t):
    ph = t * 0.4 % 1
    lift = 0.2 * (ease_io((ph - 0.5) / 0.12) - ease_io((ph - 0.88) / 0.1))
    p.arc(0, -0.25 - lift, 0.3, p.F, math.pi, math.tau)
    p.line(-0.3, -0.25 - lift, -0.3, -0.05, p.F)
    p.line(0.3, -0.25 - lift, 0.3, -0.05 - lift, p.F)
    p.box(-0.5, -0.05, 0.5, 0.72, p.A, True)
    p.disc(0, 0.25, 0.1, None)
    p.fill([(-0.04, 0.3), (0.04, 0.3), (0.07, 0.52), (-0.07, 0.52)], None)


def ic_terminal(p, t):
    p.box(-0.9, -0.7, 0.9, 0.7, p.A)
    p.line(-0.9, -0.48, 0.9, -0.48, p.A)
    for i in range(3):
        p.disc(-0.76 + i * 0.13, -0.59, 0.035, p.M)
    ph = t * 0.3 % 1 * 5
    cur = int(ph)
    for i in range(min(4, cur + 1)):
        y = -0.28 + i * 0.24
        p.path([(-0.76, y - 0.06), (-0.68, y), (-0.76, y + 0.06)], p.F)
        L = (0.4 + 0.7 * h01(i, int(t * 0.3))) * (ph % 1 if i == cur else 1)
        p.line(-0.58, y, -0.58 + L, y, p.B)
        if i == min(cur, 3) and int(t * 4) % 2:
            p.box(-0.54 + L, y - 0.07, -0.47 + L, y + 0.07, p.F, True)


def ic_star(p, t):
    s = 0.7 * (1 + 0.06 * math.sin(t * 3))
    a0 = math.sin(t * 0.8) * 0.25 - math.pi / 2
    pts = [((s if i % 2 == 0 else s * 0.42) * math.cos(a0 + i * math.pi / 5),
            (s if i % 2 == 0 else s * 0.42) * math.sin(a0 + i * math.pi / 5) + 0.04)
           for i in range(10)]
    p.fill(pts, p.A)
    p.path(pts, p.H, True)
    for i in range(5):
        tw = math.sin(t * 3 + i * 1.7)
        if tw > 0.2:
            a, r = h01(i, 9) * math.tau, 0.86
            x, y, d = r * math.cos(a), r * math.sin(a), 0.08 * tw
            p.line(x - d, y, x + d, y, p.F)
            p.line(x, y - d, x, y + d, p.F)


def ic_check(p, t):
    ph = t / 3.2 % 1
    p.arc(0, 0, 0.8, p.A, -math.pi / 2, -math.pi / 2 + math.tau * ease_io(ph / 0.3))
    for d in (-0.03, 0, 0.03):
        p.trace([(-0.38, 0.02 + d), (-0.1, 0.3 + d), (0.42, -0.3 + d)],
                ease_io((ph - 0.3) / 0.2), p.F)


def ic_warning(p, t):
    p.path([(0, -0.78), (0.86, 0.68), (-0.86, 0.68)], p.A, True)
    if t * 1.5 % 1 < 0.65:
        p.fill([(-0.07, -0.32), (0.07, -0.32), (0.035, 0.25), (-0.035, 0.25)], p.F)
        p.disc(0, 0.43, 0.07, p.F)


def ic_target(p, t):
    ph = t / 2.4 % 1
    pulse = math.exp(-(ph - 0.45) * 8) if ph > 0.45 else 0
    for i, r in enumerate((0.78, 0.53, 0.28)):
        p.arc(0, 0.05, r * (1 + 0.06 * pulse), p.M if i == 1 else p.A)
    p.disc(0, 0.05, 0.08, p.F)
    f = min(1.0, ph / 0.45) ** 2
    x, y = 0.9 - 0.9 * f, -0.85 + 0.9 * f
    p.line(x, y, x + 0.4, y - 0.4, p.F)
    p.line(x + 0.4, y - 0.4, x + 0.52, y - 0.38, p.B)
    p.line(x + 0.4, y - 0.4, x + 0.38, y - 0.52, p.B)


def ic_radar(p, t):
    for r in (0.85, 0.57, 0.28):
        p.arc(0, 0, r, p.A if r > 0.8 else p.M)
    p.line(-0.85, 0, 0.85, 0, p.M)
    p.line(0, -0.85, 0, 0.85, p.M)
    a = t * 1.6
    for k in range(9, -1, -1):
        b = a - k * 0.05
        p.line(0, 0, 0.85 * math.cos(b), 0.85 * math.sin(b), mix(p.F, p.th.bg, k / 10))
    for i in range(4):
        ang, r = h01(i, 5) * math.tau, 0.25 + 0.5 * h01(i, 6)
        age = (a - ang) % math.tau / math.tau
        if age < 0.6:
            p.disc(r * math.cos(ang), r * math.sin(ang), 0.06 * (1 - age), p.B)


def ic_atom(p, t):
    p.disc(0, 0, 0.11, p.F)
    for i in range(3):
        rot = i * math.pi / 3
        p.arc(0, 0, 0.85, p.A, ry=0.3, rot=rot)
        a = t * (1.6 + 0.3 * i) + i * 2.1
        u, v = 0.85 * math.cos(a), 0.3 * math.sin(a)
        p.disc(u * math.cos(rot) - v * math.sin(rot), u * math.sin(rot) + v * math.cos(rot),
               0.07, p.B)


def ic_cube(p, t):
    a, b = t * 0.7, t * 0.45
    V = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    P = []
    for x, y, z in V:
        x, z = x * math.cos(a) + z * math.sin(a), z * math.cos(a) - x * math.sin(a)
        y, z = y * math.cos(b) - z * math.sin(b), z * math.cos(b) + y * math.sin(b)
        f = 0.45 * 2.5 / (2.5 + z * 0.45)
        P.append((x * f, y * f, z))
    for i in range(8):
        for j in range(i + 1, 8):
            if sum(u != v for u, v in zip(V[i], V[j])) == 1:
                p.line(P[i][0], P[i][1], P[j][0], P[j][1],
                       p.A if P[i][2] + P[j][2] < 0 else p.M)
    for x, y, z in P:
        if z < 0:
            p.disc(x, y, 0.04, p.F)


def ic_git(p, t):
    br = [(-0.3, 0.45), (0.35, 0.15), (0.35, -0.2), (-0.3, -0.5)]
    p.line(-0.3, 0.85, -0.3, -0.85, p.A)
    p.path(br, p.F)
    for x, ys, c in ((-0.3, (0.78, 0.45, 0.0, -0.5, -0.8), p.A), (0.35, (0.15, -0.2), p.F)):
        for y in ys:
            p.disc(x, y, 0.08, None)
            p.arc(x, y, 0.08, c)
    x, y = along(br, t * 0.45 % 1)
    p.disc(x, y, 0.05, p.H)


def ic_bolt(p, t):
    pts = [(0.15, -0.85), (-0.4, 0.08), (-0.02, 0.08), (-0.2, 0.85), (0.42, -0.15),
           (0.04, -0.15), (0.3, -0.85)]
    ph = t % 2.4
    if ph > 0.5 or h01(int(t * 14)) > 0.45:
        p.fill(pts, p.F)
    p.path(pts, p.A, True)
    if ph < 0.5:
        for i in range(6):
            a, r = h01(i, int(t / 2.4)) * math.tau, 0.7 + ph * 0.4
            p.line(r * math.cos(a), r * math.sin(a), (r + 0.08) * math.cos(a),
                   (r + 0.08) * math.sin(a), p.H)


def ic_coffee(p, t):
    p.path([(-0.5, -0.05), (0.4, -0.05), (0.3, 0.7), (-0.4, 0.7)], p.A, True)
    p.arc(0.42, 0.25, 0.2, p.A, -math.pi / 2, math.pi / 2)
    p.line(-0.7, 0.8, 0.6, 0.8, p.M)
    for i in range(3):
        pts = [(-0.3 + i * 0.25 + 0.07 * math.sin(t * 2.2 + i + s * 0.6), -0.15 - s * 0.055)
               for s in range(12)]
        for s, (a, b) in enumerate(zip(pts, pts[1:])):
            p.line(a[0], a[1], b[0], b[1], mix(p.F, p.th.bg, s / 12))


def ic_chart(p, t):
    p.line(-0.8, -0.8, -0.8, 0.8, p.M)
    p.line(-0.8, 0.8, 0.85, 0.8, p.M)
    tops = []
    for i in range(4):
        h = 0.42 + 0.26 * i + 0.14 * math.sin(t * 1.6 + i * 1.4)
        x = -0.62 + i * 0.36
        p.box(x, 0.76 - h, x + 0.24, 0.76, p.A, True)
        tops.append((x + 0.12, 0.62 - h))
    p.path(tops, p.F)
    for x, y in tops:
        p.disc(x, y, 0.05, p.B)


def ic_cpu(p, t):
    p.box(-0.55, -0.55, 0.55, 0.55, p.A)
    for i in range(5):
        o = -0.4 + i * 0.2
        for k, seg in enumerate(((o, -0.55, o, -0.8), (o, 0.55, o, 0.8),
                                 (-0.55, o, -0.8, o), (0.55, o, 0.8, o))):
            p.line(*seg, p.F if h01(i, k, int(t * 5)) > 0.6 else p.M)
    p.box(-0.3, -0.3, 0.3, 0.3, p.F)
    s = 0.08 + 0.14 * (0.5 + 0.5 * math.sin(t * 3))
    p.box(-s, -s, s, s, p.B, True)


def ic_wifi(p, t):
    n = int(t * 2.5) % 5
    p.disc(0, 0.6, 0.08, p.F)
    for i, r in enumerate((0.42, 0.8, 1.18)):
        for d in (0, 0.04):
            p.arc(0, 0.6, r + d, p.A if i < n else p.th.faint, -math.pi * 0.75, -math.pi * 0.25)


def ic_search(p, t):
    cx, cy = 0.25 * math.cos(t * 0.9) - 0.12, 0.2 * math.sin(t * 1.3) - 0.12
    for i in range(6):
        y, x1 = -0.7 + i * 0.28, 0.2 + 0.5 * h01(i, 3)
        p.line(-0.8, y, x1, y, p.th.faint)
        if abs(y - cy) < 0.3:
            hw = math.sqrt(0.34 ** 2 - (y - cy) ** 2) * 0.9
            if max(-0.8, cx - hw) < min(x1, cx + hw):
                p.line(max(-0.8, cx - hw), y, min(x1, cx + hw), y, p.F)
    p.arc(cx, cy, 0.38, p.A)
    p.arc(cx, cy, 0.34, p.A)
    for d in (0, 0.04):
        p.line(cx + 0.27 + d, cy + 0.27 - d, cx + 0.6 + d, cy + 0.6 - d, p.F)


def ic_sync(p, t):
    a = t * 1.5
    for k in (0, 1):
        a1 = a + k * math.pi + math.pi * 0.78
        c = p.F if k else p.A
        p.arc(0, 0, 0.7, c, a + k * math.pi, a1)
        nx, ny = math.cos(a1), math.sin(a1)
        ex, ey = 0.7 * nx, 0.7 * ny
        p.fill([(ex - ny * 0.24, ey + nx * 0.24), (ex + nx * 0.15, ey + ny * 0.15),
                (ex - nx * 0.15, ey - ny * 0.15)], c)


def ic_users(p, t):
    for i, (x, sc, c) in enumerate(((-0.55, 0.72, p.M), (0.55, 0.72, p.M), (0, 1.0, p.A))):
        y = -0.3 * sc + (0.14 if sc < 1 else 0) + 0.03 * math.sin(t * 2 + i * 2)
        if sc == 1:
            p.disc(x, y, 0.2, p.F)
        else:
            p.arc(x, y, 0.2 * sc, c)
        p.arc(x, y + 0.75 * sc, 0.42 * sc, c, math.pi, math.tau)
        p.line(x - 0.42 * sc, y + 0.75 * sc, x + 0.42 * sc, y + 0.75 * sc, c)


# знак Omarchy прямоугольниками на сетке 13.5 x 13
OMA = [(0, 0, 13.5, 1), (0, 0, 1, 13), (12.5, 0, 13.5, 13), (0, 12, 7.25, 13),
       (8.5, 12, 13.5, 13), (6.25, 0, 7.25, 3), (2, 2, 7.25, 3), (2, 2, 3, 11),
       (2, 10, 11.5, 11), (10.5, 2, 11.5, 11), (9.5, 2, 11.5, 3), (0, 6, 3, 7),
       (6.25, 10, 7.25, 13)]


def ic_omarchy(p, t):
    ph = t * 0.45 % 2 - 0.3
    f = lambda x, y: (x / 13.5 * 1.7 - 0.85, y / 13 * 1.64 - 0.82)
    for x0, y0, x1, y1 in OMA:
        mid = ((x0 + x1) / 27 + (y0 + y1) / 26) / 2
        p.fill([f(x0, y0), f(x1, y0), f(x1, y1), f(x0, y1)],
               mix(p.A, p.F, 1 - abs(mid - ph) * 3))


ICONS = {
    "omarchy": ic_omarchy, "rocket": ic_rocket, "gear": ic_gear, "globe": ic_globe,
    "heart": ic_heart, "clock": ic_clock, "bulb": ic_bulb, "server": ic_server,
    "database": ic_database, "cloud": ic_cloud, "lock": ic_lock, "terminal": ic_terminal,
    "star": ic_star, "check": ic_check, "warning": ic_warning, "target": ic_target,
    "radar": ic_radar, "atom": ic_atom, "cube": ic_cube, "git": ic_git, "bolt": ic_bolt,
    "coffee": ic_coffee, "chart": ic_chart, "cpu": ic_cpu, "wifi": ic_wifi,
    "search": ic_search, "sync": ic_sync, "users": ic_users,
}


def draw_icon(scr, name, x, y, cols, rows, t, th, appear=1.0):
    dots = Dots(cols, rows)
    ICONS[name](Pen(dots, th), t)
    dots.blit(scr, x, y, None if appear >= 1 else lambda px, py: h01(px, py) < appear)


# ---------------------------------------------------------------- слайды

class Slide:
    animated = False

    def __init__(self, opts, heading, body, no):
        self.opts, self.heading, self.body, self.no = opts, heading, body, no
        self.fx = self.size = None
        self.secno = 0
        self.dur = 0.0

    def enter(self):
        """Новый показ: раскладка и случайный эффект выбираются заново."""
        self.fx = self.size = None

    def build(self, W, H, th, deck):
        self.W, self.H, self.th, self.size = W, H, th, (W, H)
        self.mx = max(3, W // 14)
        if not self.fx:
            self.fx = self.opts.get("effect") or deck.effect or deck.pick()
        self.layout()

    def place_title(self, tb, x, y, maxw, align):
        tier, cells, w, h = tb
        ox = x + ((maxw - w) // 2 if align == "c" else 0)
        self.tg = [cx / max(1, w - 1) * 0.8 + cy / max(1, h - 1) * 0.2 for cx, cy, _ in cells]
        self.tcells = [(ox + cx, y + cy, ch, self.th.grad(g))
                       for (cx, cy, ch), g in zip(cells, self.tg)]
        self.tat = BOLD if tier == "plain" else 0
        self.tdur, self.tframe = 0.0, lambda t: ()
        if cells:
            self.tdur, self.tframe = EFFECTS[self.fx](
                self.tcells, self.W, self.H, self.th, random.Random(self.no * 7919 + len(cells)))

    def draw_title(self, scr, t):
        if t < self.tdur:
            for x, y, ch, c in self.tframe(t):
                scr.put(x, y, ch, c, None, self.tat)
            return
        ph = (t - self.tdur) * 0.1          # после эффекта градиент медленно переливается
        for (x, y, ch, _), g in zip(self.tcells, self.tg):
            scr.put(x, y, ch, self.th.grad(g + ph), None, self.tat)

    def header(self):
        top = 1 if self.H < 30 else 2
        maxw = self.W - 2 * self.mx
        tb = title_block(self.heading, maxw, ("small",), "l")
        self.place_title(tb, self.mx, top, maxw, "l")
        self.rule = top + tb[3]
        return self.rule + 2

    def draw_header(self, scr, t):
        self.draw_title(scr, t)
        full = self.W - 2 * self.mx
        for i in range(int(full * ease_out((t - 0.15) / 0.7))):
            scr.put(self.mx + i, self.rule, "━", mix(self.th.accent, self.th.faint, i / full * 1.5))

    def layout(self):
        pass

    def draw(self, scr, t):
        pass


class TitleSlide(Slide):
    def layout(self):
        maxw = self.W - 2 * self.mx
        tb = title_block(self.heading, maxw, ("big", "small"), "c")
        self.sub = [styled(self.th, inline(l.strip()), self.th.muted)
                    for l in self.body if l.strip()]
        if self.sub:
            self.sub[0] = [(t, self.th.bright, at | BOLD) for t, _, at in self.sub[0]]
        n = len(self.sub)
        total = tb[3] + 2 + (n + 1 + (n > 1) if n else 0)
        y = max(1, (self.H - 2 - total) // 2)
        self.place_title(tb, self.mx, y, maxw, "c")
        self.rule, self.rw = y + tb[3] + 1, min(maxw, max(tb[2], 24))
        self.dur = max(self.tdur, 1.2 + 0.15 * n)

    def draw(self, scr, t):
        self.draw_title(scr, t)
        half = self.rw // 2
        for dx in range(int(half * ease_out((t - 0.3) / 0.8)) + 1):
            c = mix(self.th.accent, self.th.faint, dx / max(1, half) * 1.2)
            scr.put(self.W // 2 + dx, self.rule, "━", c)
            scr.put(self.W // 2 - dx, self.rule, "━", c)
        for i, runs in enumerate(self.sub):
            w = sum(width(r[0]) for r in runs)
            reveal(scr, (self.W - w) // 2, self.rule + 2 + i + (i > 0), runs,
                   t - 0.6 - i * 0.15, self.th, 90)


class SectionSlide(Slide):
    def layout(self):
        maxw = self.W - 2 * self.mx
        tb = title_block(self.heading, maxw, ("big", "small"), "l")
        self.sub = [styled(self.th, inline(l.strip()), self.th.muted)
                    for l in self.body if l.strip()]
        self.num = banner(f"{self.secno:02d}", "small")
        total = 5 + tb[3] + 2 + len(self.sub) + 1
        self.y0 = max(1, (self.H - 2 - total) // 2)
        self.place_title(tb, self.mx, self.y0 + 5, maxw, "l")
        self.rule = self.y0 + 5 + tb[3] + (1 if tb[0] == "big" else 0)
        self.dur = max(self.tdur, 1.2)

    def draw(self, scr, t):
        th = self.th
        dim = mix(th.accent, th.bg, 0.45)
        for r, row in enumerate(self.num):
            for x, ch in enumerate(row):
                if ch != " " and t > 0.1 + x * 0.02:
                    scr.put(self.mx + x, self.y0 + r, ch, mix(th.hot, dim, (t - 0.1 - x * 0.02) / 0.4))
        self.draw_title(scr, t)
        full = self.W - 2 * self.mx
        for i in range(int(full * ease_out((t - 0.3) / 0.9))):
            scr.put(self.mx + i, self.rule, "━", mix(th.accent, th.faint, i / full * 1.5))
        for i, runs in enumerate(self.sub):
            reveal(scr, self.mx, self.rule + 2 + i, runs, t - 0.6 - i * 0.15, th, 90)


class TextSlide(Slide):
    def layout(self):
        top = self.header()
        avail = self.H - 2 - top
        self.lines = airy(body_lines(self.body, min(self.W - 2 * self.mx, 100), self.th), avail)
        self.by = top + clamp((avail - len(self.lines)) // 3, 0, 3)
        self.dur = max(self.tdur, 1.1 + 0.06 * len(self.lines))

    def draw(self, scr, t):
        self.draw_header(scr, t)
        draw_lines(scr, self.mx, self.by, self.lines, t - 0.45, self.th, self.H - 2)


class SplitSlide(Slide):
    animated = True

    def layout(self):
        top = self.header()
        avail = self.H - 2 - top
        half = (self.W - 2 * self.mx - 4) // 2
        left = str(self.opts.get("side", "right")) == "left"
        self.rows = max(3, min(avail, half // 2, 26))
        self.cols = self.rows * 2
        ix = self.mx + (0 if left else half + 4)
        self.ix, self.iy = ix + (half - self.cols) // 2, top + (avail - self.rows) // 2
        self.tx = self.mx + (half + 4 if left else 0)
        self.lines = airy(body_lines(self.body, half, self.th), avail)
        self.by = top + max(0, (avail - len(self.lines)) // 2)
        self.dur = max(self.tdur, 1.4 + 0.06 * len(self.lines))

    def draw(self, scr, t):
        self.draw_header(scr, t)
        draw_lines(scr, self.tx, self.by, self.lines, t - 0.45, self.th, self.H - 2)
        if t > 0.3:
            draw_icon(scr, self.opts.get("icon", "omarchy"), self.ix, self.iy, self.cols,
                      self.rows, t, self.th, (t - 0.3) / 0.9)


class ChartSlide(Slide):
    def __init__(self, opts, heading, body, no):
        super().__init__(opts, heading, body, no)
        self.data, self.note = parse_data(body, opts)

    def layout(self):
        top = self.header()
        self.cap = styled(self.th, inline(" ".join(self.note)), self.th.muted) if self.note else None
        if self.cap:
            top += 2
        avail = self.H - 2 - top
        full = self.W - 2 * self.mx
        self.cw, self.ch = min(full, 120), min(avail, 30)
        self.cx = self.mx + (full - self.cw) // 2
        self.cy = top + (avail - self.ch) // 3
        self.top = top
        self.dur = max(self.tdur, 1.9)

    def draw(self, scr, t):
        self.draw_header(scr, t)
        if self.cap:
            reveal(scr, self.mx, self.top - 2, self.cap, t - 0.4, self.th)
        CHARTS[self.opts.get("type", "bar")](
            scr, self.cx, self.cy, self.cw, self.ch, self.data, ease_out((t - 0.5) / 1.3),
            self.th, str(self.opts.get("unit", "")), self.opts)


class TableSlide(Slide):
    def __init__(self, opts, heading, body, no):
        super().__init__(opts, heading, body, no)
        raw = [l for l in body if l.strip().startswith("|")]
        if not raw:
            raise DeckError("в таблице нет строк вида «| a | b |»")
        self.note = [l.strip() for l in body if l.strip() and not l.strip().startswith("|")]
        sep = next((split_row(l) for l in raw if TABLE_SEP.match(l)), None)
        rows = [split_row(l) for l in raw if not TABLE_SEP.match(l)]
        n = max(len(r) for r in rows)
        self.cells = [r + [""] * (n - len(r)) for r in rows]
        self.align = []
        for c in range(n):
            col = [r[c] for r in self.cells[1:] if r[c]]
            mark = sep[c] if sep and c < len(sep) else ""
            if mark.startswith(":") and mark.endswith(":"):
                self.align.append("c")
            elif mark.endswith(":") or (not mark.startswith(":") and col and all(
                    re.fullmatch(r"[-+−]?[\d\s.,]+\S{0,3}", v) for v in col)):
                self.align.append("r")
            else:
                self.align.append("l")

    def layout(self):
        top = self.header()
        self.cap = styled(self.th, inline(" ".join(self.note)), self.th.muted) if self.note else None
        if self.cap:
            top += 2
        self.top = top
        avail, full = self.H - 2 - top, self.W - 2 * self.mx
        n = len(self.align)
        ws = [max(width(r[c]) for r in self.cells) for c in range(n)]
        room = full - (n + 1) - 2 * n
        while sum(ws) > room and max(ws) > 4:
            ws[ws.index(max(ws))] -= 1
        pad = 1 + int(clamp((room - sum(ws)) // (2 * n), 0, 3))
        self.ws = [w + 2 * pad for w in ws]
        self.pad = pad
        self.grid = str(self.opts.get("grid", "")).lower() in ("1", "yes", "true", "on")
        per = 2 if self.grid else 1          # с линейками ряд занимает две строки
        self.shown = self.cells[:max(2, (avail - 4) // per + per - 1)]
        self.more = len(self.cells) - len(self.shown)
        self.tw = sum(self.ws) + n + 1
        self.tx = self.mx + (full - self.tw) // 2
        height = len(self.shown) + 3 + (max(0, len(self.shown) - 2) if self.grid else 0)
        self.ty = top + clamp((avail - height - 1) // 3, 0, 4)
        self.dur = max(self.tdur, 1.1 + 0.08 * len(self.shown))

    def draw(self, scr, t):
        th = self.th
        self.draw_header(scr, t)
        if self.cap:
            reveal(scr, self.mx, self.top - 2, self.cap, t - 0.4, th)
        lim = self.tx + self.tw * ease_out((t - 0.4) / 0.6)

        def rule(y, l, m, r):
            s = l + m.join("─" * w for w in self.ws) + r
            for i, ch in enumerate(s):
                if self.tx + i < lim:
                    scr.put(self.tx + i, y, ch, th.faint)

        rule(self.ty, "╭", "┬", "╮")
        y = self.ty + 1
        for ri, row in enumerate(self.shown):
            if ri == 1 or (ri > 1 and self.grid):
                rule(y, "├", "┼", "┤")
                y += 1
            x = self.tx
            for c, cell in enumerate(row):
                if x < lim:
                    scr.put(x, y, "│", th.faint)
                inner = self.ws[c] - 2 * self.pad
                txt = clip(cell, inner)
                off = {"l": 0, "r": inner - width(txt), "c": (inner - width(txt)) // 2}[self.align[c]]
                runs = [(txt, th.bright, BOLD)] if ri == 0 else styled(th, inline(txt), th.fg)
                reveal(scr, x + 1 + self.pad + off, y, runs, t - 0.5 - ri * 0.08, th)
                x += self.ws[c] + 1
            if x < lim:
                scr.put(x, y, "│", th.faint)
            y += 1
        rule(y, "╰", "┴", "╯")
        if self.more > 0:
            scr.text(self.tx + 1, y + 1, f"… ещё строк: {self.more}", th.muted)


class IconsSlide(Slide):
    """Галерея всего набора иконок."""
    animated = True

    def layout(self):
        top = self.header()
        avail, full = self.H - 2 - top, self.W - 2 * self.mx
        for rows in (8, 6, 5, 4, 3):
            self.rows, self.cellw, self.cellh = rows, rows * 2 + 4, rows + 2
            self.per = max(1, full // self.cellw)
            if math.ceil(len(ICONS) / self.per) * self.cellh <= avail:
                break
        self.gx = self.mx + (full - self.per * self.cellw) // 2
        self.gy = top
        self.dur = max(self.tdur, 1.5)

    def draw(self, scr, t):
        self.draw_header(scr, t)
        for i, name in enumerate(ICONS):
            x = self.gx + i % self.per * self.cellw
            y = self.gy + i // self.per * self.cellh
            if y + self.cellh > self.H - 1:
                break
            ap = (t - 0.3 - i * 0.03) / 0.6
            if ap > 0:
                draw_icon(scr, name, x + 2, y, self.rows * 2, self.rows, t, self.th, ap)
                scr.text(x + 2 + (self.rows * 2 - len(name)) // 2, y + self.rows, name,
                         self.th.muted)


KINDS = {"title": TitleSlide, "section": SectionSlide, "text": TextSlide, "split": SplitSlide,
         "chart": ChartSlide, "table": TableSlide, "icons": IconsSlide}
META = {"title", "author", "date", "effect"}


# ---------------------------------------------------------------- файл презентации

class Deck:
    def __init__(self, slides, meta, path=None):
        self.slides, self.meta, self.path = slides, meta, path
        self.title = meta.get("title") or (slides[0].heading if slides else "")
        self.effect = meta.get("effect")
        self.bag = []
        n = 0
        for s in slides:
            if isinstance(s, SectionSlide):
                n += 1
                s.secno = n

    def pick(self):
        if not self.bag:
            self.bag = list(EFFECTS)
            random.shuffle(self.bag)
        return self.bag.pop()


def parse_opts(kind, tokens):
    opts = {}
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            opts[k.strip().lower()] = v
        elif tok in EFFECTS:
            opts["effect"] = tok
        elif kind == "split" and tok in ICONS:
            opts["icon"] = tok
        elif kind == "split" and tok in ("left", "right"):
            opts["side"] = tok
        elif kind == "chart" and tok in CHARTS:
            opts["type"] = tok
        elif kind == "table" and tok == "grid":
            opts["grid"] = "1"
        else:
            raise DeckError(f"непонятный параметр «{tok}»")
    if opts.get("effect", "decrypt") not in EFFECTS:
        raise DeckError(f"нет эффекта «{opts['effect']}»; есть: {', '.join(EFFECTS)}")
    if kind == "split" and opts.get("icon", "omarchy") not in ICONS:
        raise DeckError(f"нет иконки «{opts['icon']}»; есть: {', '.join(ICONS)}")
    if kind == "chart" and opts.get("type", "bar") not in CHARTS:
        raise DeckError(f"нет графика «{opts['type']}»; есть: {', '.join(CHARTS)}")
    for k in ("min", "max"):
        if k in opts:
            try:
                opts[k] = num(opts[k])
            except ValueError:
                raise DeckError(f"{k}= должно быть числом")
    return opts


def parse_deck(text, path=None):
    chunks, cur, fence = [], [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        if not fence and re.fullmatch(r"\s*-{3,}\s*", line):
            chunks.append(cur)
            cur = []
        else:
            cur.append(line)
    chunks.append(cur)

    meta, slides = {}, []
    if chunks:
        first = [l for l in chunks[0] if l.strip()]
        pairs = [re.fullmatch(r"\s*(\w+)\s*:\s*(.*)", l) for l in first]
        if first and all(m and m.group(1).lower() in META for m in pairs):
            meta = {m.group(1).lower(): m.group(2).strip() for m in pairs}
            chunks = chunks[1:]
    if meta.get("effect") and meta["effect"] not in EFFECTS:
        raise DeckError(f"нет эффекта «{meta['effect']}»; есть: {', '.join(EFFECTS)}")
    for chunk in chunks:
        lines = [l for l in chunk if not l.lstrip().startswith("//")]
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue
        no = len(slides) + 1
        try:
            kind, tokens = None, []
            if lines[0].lstrip().startswith("@"):
                tokens = shlex.split(lines.pop(0).strip()[1:])
                kind = tokens.pop(0).lower() if tokens else ""
                if kind not in KINDS:
                    raise DeckError(f"нет вида слайда «@{kind}»; есть: {', '.join(KINDS)}")
            while lines and not lines[0].strip():
                lines.pop(0)
            heading = ""
            if lines and re.match(r"\s*#\s", lines[0]):
                heading = lines.pop(0).strip()[1:].strip()
            if kind is None:
                kind = "table" if any(l.strip().startswith("|") for l in lines) else "text"
            slides.append(KINDS[kind](parse_opts(kind, tokens), heading, lines, no))
            slides[-1].raw = "\n".join(chunk).strip("\n")
        except (DeckError, ValueError) as e:
            raise DeckError(f"слайд {no}: {e}")
    if not slides:
        raise DeckError("в презентации нет ни одного слайда")
    return Deck(slides, meta, path)


def load_deck(path):
    try:
        with open(path, encoding="utf-8") as f:
            return parse_deck(f.read(), path)
    except (OSError, UnicodeDecodeError) as e:
        raise DeckError(f"не удалось прочитать {path}: {e}")


DEMO = """\
title: omadeck
author: демо

---
@title
# OMADECK
Презентации прямо в терминале
в духе Omarchy  ·  → дальше  ·  ? справка

---
@section
# Слайды
От титульного до графика — всё рисуется символами

---
# Просто текст
Обычный абзац сам переносится по ширине окна. Внутри строки работают **жирный**, *курсив* и `код`.

- пункты списка появляются по очереди
- их можно вкладывать
  - вот так
- а можно нумеровать

1. первый шаг
2. второй шаг

> Заголовок каждого слайда появляется со случайным эффектом — как на заставке Omarchy.

---
@split icon=rocket
# Текст и иконка
Текст занимает половину экрана, на второй половине — анимированная иконка из набора.

- `@split icon=rocket`
- `side=left` переносит иконку влево
- весь набор: `omadeck icons`

---
@split icon=cube side=left
# Иконка слева
Иконки рисуются точками Брайля и перекрашиваются под текущую тему.

- cube, atom, radar, globe
- server, database, cloud, git
- и ещё два десятка

---
@chart type=bar unit=%
# Столбцы
Доля деплоев без отката по кварталам
Q1: 71
Q2: 78
Q3: 86
Q4: 94

---
@chart type=line
# Линии
| Месяц | 2025 | 2026 |
| Янв | 12 | 18 |
| Фев | 15 | 24 |
| Мар | 14 | 31 |
| Апр | 22 | 29 |
| Май | 28 | 41 |
| Июн | 26 | 52 |

---
@chart type=donut
# Кольцо
Где живут сервисы
Kubernetes: 46
Bare metal: 21
VM: 18
Serverless: 9

---
@chart type=hbar unit=" мс"
# Полосы
p95 ответа по сервисам
api-gateway: 42
auth: 67
billing: 118
search: 203

---
@table
# Таблица
| Сервис | Реплик | CPU | Статус |
|---|---|---|---|
| api-gateway | 6 | 41% | **ok** |
| auth | 3 | 18% | **ok** |
| billing | 4 | 63% | деградация |
| search | 8 | 77% | **ok** |

---
@section
# Своя презентация
Мастер или обычный текстовый файл

---
# Формат файла
Слайды разделяются строкой `---`, вид задаёт первая строка с `@`.

```
@split icon=rocket
# Заголовок слайда
- пункт списка

---
@chart type=bar
# Продажи
Январь: 12
Февраль: 19
```

- `omadeck new talk.deck` — собрать в мастере
- `omadeck talk.deck` — показать, клавиша `r` перечитывает файл

---
@title effect=decrypt
# Спасибо
omadeck demo  ·  omadeck new  ·  omadeck icons
"""


# ---------------------------------------------------------------- показ

HELP = [
    ("→  пробел  Enter  l", "дальше"),
    ("←  Backspace  h", "назад"),
    ("g / G", "первый / последний слайд"),
    ("число, Enter", "перейти к слайду"),
    ("r", "перечитать файл и повторить слайд"),
    ("?", "эта справка"),
    ("q  Esc", "выход"),
]
NEXT = {" ", "\r", "\n", "l", "j", "n", "\x1b[C", "\x1b[B", "\x1b[6~", "\x1bOC", "\x1bOB"}
PREV = {"h", "k", "p", "\x7f", "\x08", "\x1b[D", "\x1b[A", "\x1b[5~", "\x1bOD", "\x1bOA"}
FIRST = {"g", "\x1b[H", "\x1b[1~", "\x1bOH"}
LAST = {"G", "\x1b[F", "\x1b[4~", "\x1bOF"}


def render(deck, th, i, W, H, t, show_help=False, msg=""):
    scr = Screen(W, H)
    if W < 44 or H < 14:
        scr.text(1, 1, "окно слишком маленькое", th.muted)
        return scr
    sl = deck.slides[i]
    if sl.size != (W, H):
        sl.build(W, H, th, deck)
    sl.draw(scr, t)
    n, mx = len(deck.slides), max(3, W // 14)
    cnt = f"{i + 1}/{n}"
    scr.text(mx, H - 1, clip(msg or deck.title, W // 3), th.hot if msg else th.faint)
    scr.text(W - mx - len(cnt), H - 1, cnt, th.muted)
    bw = min(40, W // 4)
    for q in range(bw):
        scr.put((W - bw) // 2 + q, H - 1, "━", th.accent if q < round(bw * (i + 1) / n) else th.faint)
    if show_help:
        kw = max(width(k) for k, _ in HELP)
        bw = kw + max(width(d) for _, d in HELP) + 7
        bx, by = (W - bw) // 2, (H - len(HELP) - 2) // 2
        for r in range(len(HELP) + 2):
            for q in range(bw):
                edge = r in (0, len(HELP) + 1)
                ch = "─" if edge else " "
                if q in (0, bw - 1):
                    ch = ("╭╮" if r == 0 else "╰╯")[q > 0] if edge else "│"
                scr.put(bx + q, by + r, ch, th.muted)
        for r, (k, d) in enumerate(HELP):
            scr.text(bx + 2, by + 1 + r, k, th.accent, BOLD)
            scr.text(bx + kw + 5, by + 1 + r, d, th.fg)
    return scr


def parse_keys(data):
    i = 0
    while i < len(data):
        if data[i] == "\x1b":
            m = re.match(r"\x1b[\[O][0-9;]*[A-Za-z~]", data[i:])
            if m:
                yield m.group()
                i += m.end()
                continue
        yield data[i]
        i += 1


class Player:
    def __init__(self, deck, th, start=0):
        self.deck, self.th = deck, th
        self.help, self.msg, self.msg_until = False, "", 0.0
        self.goto(start)

    def goto(self, i):
        self.i = int(clamp(i, 0, len(self.deck.slides) - 1))
        self.deck.slides[self.i].enter()
        self.t0 = time.monotonic()

    def say(self, msg):
        self.msg, self.msg_until = msg, time.monotonic() + 4

    def reload(self):
        if self.deck.path:
            try:
                deck = load_deck(self.deck.path)
                deck.effect = self.deck.effect
                self.deck = deck
            except DeckError as e:
                self.say(str(e))
        self.goto(self.i)

    def key(self, key, num):
        """Обрабатывает клавишу; возвращает False для выхода."""
        if key in ("q", "\x1b", "\x03"):
            return False
        sl = self.deck.slides[self.i]
        t = time.monotonic() - self.t0
        if num and key in ("\r", "\n", "g"):
            self.goto(int(num) - 1)
        elif key in NEXT:
            if t < sl.dur:               # первое нажатие доигрывает анимацию
                self.t0 -= sl.dur - t
            elif self.i < len(self.deck.slides) - 1:
                self.goto(self.i + 1)
        elif key in PREV:
            self.goto(self.i - 1)
        elif key in FIRST:
            self.goto(0)
        elif key in LAST:
            self.goto(len(self.deck.slides) - 1)
        elif key == "r":
            self.reload()
        elif key == "?":
            self.help = not self.help
        return True

    def run(self):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise DeckError("для показа нужен терминал")
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        out = sys.stdout
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda *_: sys.exit(0))
        try:
            tty.setcbreak(fd)
            out.write("\x1b[?1049h\x1b[?25l\x1b[?7l")
            self.loop(fd, out)
        except KeyboardInterrupt:
            pass
        finally:
            out.write("\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
            out.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def loop(self, fd, out):
        prev, size, num = None, None, ""
        while True:
            now = time.monotonic()
            sz = os.get_terminal_size(fd)
            if (sz.columns, sz.lines) != size:
                size, prev = (sz.columns, sz.lines), None
                out.write("\x1b[2J")
            msg = self.msg if now < self.msg_until else ("→ " + num if num else "")
            lines = render(self.deck, self.th, self.i, size[0], size[1], now - self.t0,
                           self.help, msg).lines()
            buf = ["\x1b[?2026h"]
            for y, l in enumerate(lines):
                if prev is None or prev[y] != l:
                    buf.append(f"\x1b[{y + 1};1H{l}")
            buf.append("\x1b[?2026l")
            if len(buf) > 2:
                out.write("".join(buf))
                out.flush()
            prev = lines
            sl = self.deck.slides[self.i]
            busy = sl.animated or now - self.t0 < sl.dur + 0.3
            if not select.select([fd], [], [], 1 / 30 if busy else 1 / 10)[0]:
                continue
            for key in parse_keys(os.read(fd, 64).decode("utf-8", "ignore")):
                key = RU.get(key, key)
                if key.isdigit():
                    num = (num + key)[-4:]
                    continue
                if not self.key(key, num):
                    return
                num = ""


# ---------------------------------------------------------------- мастер

BACK = object()     # ответ «<» в мастере: вернуться на шаг назад
HEAD = ("head", "Заголовок", "str")
BODY = ("body", "Текст; «- » — пункт списка, «.» — пустая строка", "lines", None)
FIELDS = {
    "title": [("head", "Название презентации", "str", "Презентация"),
              ("sub", "Подзаголовок", "str", ""),
              ("author", "Автор", "str", os.environ.get("USER", ""))],
    "section": [HEAD + ("Раздел",), ("sub", "Подзаголовок", "str", "")],
    "text": [HEAD + ("Текст",), BODY],
    "split": [HEAD + ("Текст и иконка",), ("icon", "Иконка", list(ICONS), "rocket"),
              ("side", "Где иконка", ["right", "left"], "right"), BODY],
    "chart": [HEAD + ("График",), ("type", "Вид", list(CHARTS), "bar"),
              ("unit", "Единица измерения (необязательно)", "str", ""),
              ("data", "Данные строками «метка: значение»", "data", None)],
    "table": [HEAD + ("Таблица",), ("cols", "Колонки через |", "str", "Параметр | Значение"),
              ("rows", "Ряды таблицы, ячейки через |", "lines", None),
              ("grid", "Линейки между рядами", ["n", "y"], "n")],
}
KIND_NAMES = {"title": "титул", "section": "раздел", "text": "текст", "split": "текст + иконка",
              "chart": "график", "table": "таблица", "icons": "иконки"}


def slide_record(sl):
    """Слайд из файла → запись мастера. Нетронутые слайды пишутся обратно как были (raw)."""
    kind = next(k for k, c in KINDS.items() if type(sl) is c)
    body = [l.rstrip() for l in sl.body]
    while body and not body[-1]:
        body.pop()
    while body and not body[0]:
        body.pop(0)
    full = [l.strip() for l in body if l.strip()]
    opts = dict(sl.opts)
    r = {"kind": kind, "head": sl.heading, "raw": sl.raw, "more": []}
    if kind == "title":
        r.update(sub="".join(full[:1]), author="".join(full[1:2]), more=full[2:])
    elif kind == "section":
        r.update(sub="".join(full[:1]), more=full[1:])
    elif kind in ("text", "split"):
        r["body"] = [l or "." for l in body]
        if kind == "split":
            r.update(icon=opts.pop("icon", "omarchy"), side=opts.pop("side", "right"))
    elif kind == "chart":
        r.update(type=opts.pop("type", "bar"), unit=opts.pop("unit", ""), data=full)
    elif kind == "table":
        tb = [l for l in full if l.startswith("|")]
        rows = [unpipe(l) for l in tb if not TABLE_SEP.match(l)]
        r.update(cols=rows[0], rows=rows[1:], sep=[l for l in tb if TABLE_SEP.match(l)][:1],
                 grid="y" if str(opts.pop("grid", "")).lower() in ("1", "yes", "true", "on") else "n",
                 more=[l for l in full if not l.startswith("|")])
    r["extra"] = "".join(f" {k}={shlex.quote(fmt(v) if isinstance(v, float) else str(v))}"
                         for k, v in opts.items())
    return r


def slide_text(s):
    if s.get("raw") is not None:
        return s["raw"]
    k, x = s["kind"], s.get("extra", "")
    head = ["# " + s["head"]] if s["head"] else []
    blank = lambda ls: ["" if l.strip() == "." else l for l in ls]
    if k == "title":
        lines = ["@title" + x] + head + [v for v in (s["sub"], s["author"]) if v]
    elif k == "section":
        lines = ["@section" + x] + head + [v for v in (s["sub"],) if v]
    elif k == "text":
        explicit = x or any(l.lstrip().startswith("|") for l in s["body"])
        lines = (["@text" + x] if explicit else []) + head + blank(s["body"])
    elif k == "split":
        lines = [f"@split icon={s['icon']} side={s['side']}{x}"] + head + blank(s["body"])
    elif k == "chart":
        unit = f" unit={shlex.quote(s['unit'])}" if s["unit"] else ""
        lines = [f"@chart type={s['type']}{unit}{x}"] + head + s["data"]
    else:
        lines = (["@table" + (" grid" if s["grid"] == "y" else "") + x] + head +
                 [f"| {unpipe(s['cols'])} |"] + s.get("sep", []) +
                 [f"| {unpipe(r)} |" for r in s["rows"]])
    return "\n".join(lines + s.get("more", []))


def wizard(path, th, deck=None):
    """Собирает презентацию по вопросам (или правит готовую) и записывает файл."""
    try:
        import readline          # правка строки и подстановка прежнего значения
    except ImportError:
        readline = None
    tint = lambda c, s: "\001\x1b[38;2;%d;%d;%dm\002%s\001\x1b[0m\002" % (*c, s)
    mark = tint(th.accent, "▪ ")

    def read(prompt, prefill=""):
        if readline and prefill:
            readline.set_startup_hook(lambda: readline.insert_text(prefill))
        try:
            return input(prompt)
        finally:
            if readline:
                readline.set_startup_hook()

    def ask(q, default="", prefill=""):
        if prefill and not readline:
            default, prefill = prefill, ""
        hint = tint(th.muted, f" [{default}]") if default and not prefill else ""
        v = read(mark + q + hint + ": ", prefill).strip()
        # «<» можно дописать и к подставленному значению, не стирая его
        return BACK if v in ("<", prefill.strip() + "<") else v or default

    def choose(q, options, default):
        while True:
            v = ask(f"{q} ({', '.join(options)})", default)
            if v is BACK or v in options:
                return v
            print(tint(th.hot, "  такого нет"))

    def many(q, old):
        """Строки по одной; прежние подставляются для правки, стёртая — удаляется."""
        lines, i = list(old), 0
        print(mark + q + tint(th.muted, " (пустая строка — конец)"))
        if lines and not readline:
            print("\n".join(tint(th.faint, "  │ ") + l for l in lines))
            v = ask("Оставить как есть? (y/n)", "y")
            if v is BACK or v == "y":
                return v if v is BACK else lines
            lines = []
        while True:
            pre = lines[i] if i < len(lines) else ""
            line = read(tint(th.faint, "  │ "), pre).rstrip()
            if line.strip() == "<" or line == pre + "<":
                if i == 0:
                    return BACK
                i -= 1
            elif line:
                lines[i:i + 1] = [line]
                i += 1
            elif i < len(lines):
                del lines[i]
            else:
                return lines

    def form(kind, cur=None):
        """Вопросы по слайду; «<» на первом вопросе отменяет — тогда None."""
        if kind not in FIELDS:
            print(tint(th.hot, "  этот вид слайда правится только в файле"))
            return None
        vals, fields, i = dict(cur or {"kind": kind}), FIELDS[kind], 0
        while i < len(fields):
            key, q, typ, dflt = fields[i]
            old = vals.get(key)
            if key == "icon":
                print(tint(th.muted, "  " + " ".join(ICONS)))
            if isinstance(typ, list):
                v = choose(q, typ, old or dflt)
            elif typ == "str":
                v = ask(q, dflt, old or "")
            else:
                v = many(q, old or [])
            if v is BACK:
                if i == 0:
                    return None
                i -= 1
                continue
            vals[key] = v
            if typ == "data":
                try:
                    parse_data(v, {})
                except DeckError as e:
                    print(tint(th.hot, f"  {e}"))
                    continue
            i += 1
        return vals

    print()
    for row in banner("omadeck", "small"):
        print("  " + tint(th.accent, row))
    print(tint(th.muted, "  мастер презентаций · «<» в любом ответе — на шаг назад"
                         " · Ctrl+C — отмена без сохранения\n"))
    if deck:
        meta, slides = dict(deck.meta), [slide_record(s) for s in deck.slides]
    else:
        first = None
        while not first:
            first = form("title")
        meta, slides = {"title": first["head"], "author": first["author"]}, [first]
    kinds = list(FIELDS)[1:]
    key = lambda s: tint(th.accent, s)
    while True:
        print()
        for n, s in enumerate(slides, 1):
            print(f"  {key(f'{n:>2}')}  {tint(th.muted, KIND_NAMES[s['kind']].ljust(15))} {s['head']}")
        print(f"\n  {key('+')} добавить  {key('N')} править  {key('-N')} удалить  "
              f"{key('N>K')} переместить  {key('Enter')} записать")
        cmd = read(mark + "Что дальше: ").strip().replace(" ", "")
        n = len(slides)
        if cmd.isdigit() and 1 <= int(cmd) <= n:
            old = slides[int(cmd) - 1]
            new = form(old["kind"], old)
            if new and {**new, "raw": None} != {**old, "raw": None}:
                new["raw"] = None
                slides[int(cmd) - 1] = new
                for mk, sk in (("title", "head"), ("author", "author")):    # подвал вслед за титулом
                    if old["kind"] == "title" and meta.get(mk) and meta[mk] == old[sk]:
                        meta[mk] = new[sk]
        elif cmd in ("+", "a", "д"):
            v = ask("Вид слайда: " + "  ".join(f"{i} {KIND_NAMES[k]}" for i, k in enumerate(kinds, 1)), "2")
            if v is not BACK and v.isdigit() and 1 <= int(v) <= len(kinds):
                s = form(kinds[int(v) - 1])
                if s:
                    slides.append(s)
        elif re.fullmatch(r"-\d+", cmd) and 1 <= int(cmd[1:]) <= n and n > 1:
            del slides[int(cmd[1:]) - 1]
        elif re.fullmatch(r"\d+>\d+", cmd) and all(1 <= int(v) <= n for v in cmd.split(">")):
            a, b = (int(v) - 1 for v in cmd.split(">"))
            slides.insert(b, slides.pop(a))
        elif cmd:
            print(tint(th.hot, "  не понял; номера — от 1 до %d, последний слайд удалить нельзя" % n))
        elif deck:
            if ask(f"Записать изменения в {path}? (y/n)", "y") == "y":
                out = path
                break
        else:
            slug = re.sub(r"[^\w-]+", "-", slides[0]["head"].lower()).strip("-") or "talk"
            out = path or ask("Файл", slug + ".deck")
            while out is not BACK and os.path.exists(out):
                v = ask(f"{out} уже есть, перезаписать? (y/n)", "n")
                if v == "y":
                    break
                out = BACK if v is BACK else ask("Тогда другой файл", "<")
            if out is not BACK:
                break
    head = "\n".join(f"{k}: {v}" for k, v in meta.items() if v)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n\n---\n".join(([head] if head else []) + [slide_text(s) for s in slides]) + "\n")
    print(tint(th.accent, f"\n  записано: {out}") + tint(th.muted, f"  ·  показ: omadeck {out}"))
    return out if ask("Показать сейчас? (y/n)", "y") == "y" else None


# ---------------------------------------------------------------- запуск

def main():
    cmds = ("run", "new", "edit", "demo", "sample", "icons", "effects", "dump")
    argv = sys.argv[1:]
    if argv and argv[0] not in cmds and not argv[0].startswith("-"):
        argv.insert(0, "run")
    ap = argparse.ArgumentParser(prog="omadeck", description=__doc__.split("\n")[0])
    ap.add_argument("-V", "--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", metavar="команда")

    def cmd(name, text, file=None):
        p = sub.add_parser(name, help=text, description=text)
        if file:
            p.add_argument("file", nargs=file, help="файл презентации")
        p.add_argument("--theme", metavar="colors.toml", help="взять цвета из другой темы")
        return p

    def show(p):
        p.add_argument("-s", "--slide", type=int, default=1, metavar="N", help="с какого слайда")
        p.add_argument("-e", "--effect", choices=list(EFFECTS), help="один эффект для всех заголовков")
        return p

    show(cmd("run", "показать презентацию (можно просто: omadeck FILE)", 1))
    cmd("new", "собрать презентацию в мастере", "?")
    cmd("edit", "править готовую презентацию в мастере", 1)
    show(cmd("demo", "показать встроенную демонстрацию"))
    cmd("sample", "записать демонстрацию в файл как образец", "?")
    cmd("icons", "показать набор иконок")
    cmd("effects", "перечислить эффекты заголовков")
    d = show(cmd("dump", "напечатать один кадр слайда в stdout", "?"))
    d.add_argument("-t", "--time", type=float, default=99.0, help="момент анимации, секунды")
    d.add_argument("--size", default="110x32", metavar="WxH")
    d.add_argument("--ansi", action="store_true", help="с цветами")
    args = ap.parse_args(argv)

    try:
        if not args.cmd:
            ap.print_help()
            return 0
        if args.cmd == "effects":
            print("\n".join(EFFECTS))
            return 0
        th = Theme(args.theme)
        file = (getattr(args, "file", None) or [None])
        file = file[0] if isinstance(file, list) else file
        if args.cmd == "sample":
            file = file or "demo.deck"
            if os.path.exists(file):
                raise DeckError(f"{file} уже существует")
            with open(file, "w", encoding="utf-8") as f:
                f.write(DEMO)
            print(f"записано: {file}")
            return 0
        if args.cmd in ("new", "edit"):
            file = wizard(file, th, load_deck(file) if args.cmd == "edit" else None)
            if not file:
                return 0
            deck = load_deck(file)
        elif args.cmd == "icons":
            deck = parse_deck("@icons\n# Иконки\n")
        elif args.cmd == "demo" or (args.cmd == "dump" and not file):
            deck = parse_deck(DEMO)
        else:
            deck = load_deck(file)
        deck.effect = getattr(args, "effect", None) or deck.effect
        start = getattr(args, "slide", 1) - 1
        if args.cmd == "dump":
            W, H = (int(v) for v in args.size.lower().split("x"))
            i = int(clamp(start, 0, len(deck.slides) - 1))
            print("\n".join(render(deck, th, i, W, H, args.time).lines(args.ansi)))
            return 0
        Player(deck, th, start).run()
    except DeckError as e:
        print(f"omadeck: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
