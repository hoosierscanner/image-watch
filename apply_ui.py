"""The apply.py checklist: a small model (testable) and a curses screen."""
import curses
import time

from apply_status import Item, status_text


class Checklist:
    def __init__(self, items):
        self.items = items
        self.cursor = 0

    def move(self, delta):
        self.cursor = max(0, min(len(self.items) - 1, self.cursor + delta))

    def toggle(self):
        item = self.items[self.cursor]
        if item.error is None:
            item.selected = not item.selected

    def select_pending(self):
        for i in self.items:
            if i.error is None:
                i.selected = i.kind is not None

    def clear(self):
        for i in self.items:
            i.selected = False

    def chosen(self):
        return [i for i in self.items if i.selected]

    def handle_key(self, key):
        if key in (10, 13, curses.KEY_ENTER):
            return "done"
        if key == ord("q"):
            return "quit"
        if key in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif key == ord(" "):
            self.toggle()
        elif key == ord("a"):
            self.select_pending()
        elif key == ord("n"):
            self.clear()
        return None


def row_line(item, current, width):
    line = ("> " if current else "  ") + ("[x] " if item.selected else "[ ] ") + item.name.ljust(26) + " " + status_text(item)
    return line[:max(width - 1, 0)]


def detail_lines(item):
    if item.error:
        return [item.error]
    lines = []
    n = item.notice
    if n:
        lines += list(n.get("flags") or [])
        first = next((ln for ln in (n.get("summary") or "").splitlines() if ln.strip()), None)
        if first:
            lines.append(first)
        if n.get("at"):
            lines.append("announced " + n["at"])
    elif item.kind:
        lines.append("no saved summary" if item.announced else "not announced: no summary, PRs not checked")
    return lines[:4]


def _put(stdscr, y, text, attr, width):
    try:
        stdscr.addstr(y, 0, text[:max(width - 1, 0)], attr)
    except curses.error:
        pass


def _draw(stdscr, cl):
    height, width = stdscr.getmaxyx()
    _put(stdscr, 0, "image-watch apply — " + time.strftime("%H:%M"), curses.A_BOLD, width)
    y = 2
    for idx, item in enumerate(cl.items):
        if y >= height - 1:
            break
        attr = curses.A_DIM if item.error else (curses.A_BOLD if item.kind else 0)
        if idx == cl.cursor:
            attr |= curses.A_REVERSE
        _put(stdscr, y, row_line(item, idx == cl.cursor, width), attr, width)
        y += 1
    y += 1
    for line in (detail_lines(cl.items[cl.cursor]) if cl.items else []):
        if y >= height - 1:
            break
        _put(stdscr, y, line, 0, width)
        y += 1
    if height > 1:
        _put(stdscr, height - 1, "↑↓ move  space toggle  a all pending  n none  enter continue  q quit", 0, width)


def run_checklist(items):
    """Show the checklist; return the chosen items on Enter, None on q."""
    cl = Checklist(items)

    def loop(stdscr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        while True:
            stdscr.erase()
            _draw(stdscr, cl)
            stdscr.refresh()
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            r = cl.handle_key(key)
            if r == "done":
                return cl.chosen()
            if r == "quit":
                return None

    return curses.wrapper(loop)
