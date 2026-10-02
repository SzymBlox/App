"""RobloxTools for Android - browse, install and export Roblox Studio plugins.

Pages: Browse (store) - Library (installed plugins) - Export - Settings
       (+ catalog manager for admins: plugin list and a phone-sized editor)

Folders (internal storage):
    /storage/emulated/0/RobloxTools/plugins            plugins downloaded from the store
    /storage/emulated/0/RobloxTools/exported plugins   result of an export
"""

import os
import re
import time
import hashlib
import threading

from kivy.config import Config
Config.set("kivy", "exit_on_escape", "0")

from kivy.app import App
from kivy.clock import Clock
from kivy.animation import Animation
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.utils import get_color_from_hex, platform
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.carousel import Carousel
from kivy.uix.checkbox import CheckBox
from kivy.uix.image import AsyncImage
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.progressbar import ProgressBar
from kivy.uix.screenmanager import ScreenManager, Screen, FadeTransition
from kivy.uix.scrollview import ScrollView
from kivy.uix.switch import Switch
from kivy.uix.textinput import TextInput

import core
from core import ApiError, DownloadError

# ---------------------------------------------------------------------------
# Theme + scaling (the two "Interface" settings feed S() and F())
# ---------------------------------------------------------------------------

BG_APP, BG_SIDEBAR, BG_CARD, BG_CARD_ALT, BG_HOVER = "#0e1014", "#13151a", "#181b21", "#20242c", "#262b34"
TEXT, TEXT_2, MUTED = "#eef0f4", "#c4c9d4", "#7c8495"
ACCENT, ACCENT_SOFT = "#5b8cff", "#1a2440"
SUCCESS, SUCCESS_SOFT = "#3dd68c", "#132a21"
WARNING, WARNING_SOFT = "#f5b544", "#33280f"
DANGER, DANGER_SOFT = "#f0605d", "#351a1b"


def C(hex_color):
    return get_color_from_hex(hex_color)


class UI:
    scale = 1.0   # sizes of buttons, cards, spacing
    text = 1.0    # text size on top of the scale


def S(v):
    return dp(v) * UI.scale


def F(v):
    return sp(v) * UI.scale * UI.text


def ui(fn, *args):
    """Run fn on the UI thread."""
    Clock.schedule_once(lambda dt: fn(*args), 0)


def bg(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def time_ago(ts):
    if not ts:
        return ""
    try:
        delta = max(time.time() - float(ts), 0)
    except (TypeError, ValueError):
        return ""
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    days = int(delta // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


# ---------------------------------------------------------------------------
# Storage access (Android 11+ needs "All files access" for custom folders)
# ---------------------------------------------------------------------------

def has_storage_access():
    if platform != "android":
        return True
    try:
        from jnius import autoclass
        if autoclass("android.os.Build$VERSION").SDK_INT >= 30:
            return bool(autoclass("android.os.Environment").isExternalStorageManager())
        from android.permissions import check_permission, Permission
        return bool(check_permission(Permission.WRITE_EXTERNAL_STORAGE))
    except Exception:
        return True


def request_storage_access():
    if platform != "android":
        return
    try:
        from jnius import autoclass
        from android.permissions import request_permissions, Permission
        request_permissions([Permission.READ_EXTERNAL_STORAGE, Permission.WRITE_EXTERNAL_STORAGE])
        if autoclass("android.os.Build$VERSION").SDK_INT >= 30 and not has_storage_access():
            Intent = autoclass("android.content.Intent")
            Settings = autoclass("android.provider.Settings")
            Uri = autoclass("android.net.Uri")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
            intent.setData(Uri.parse("package:" + activity.getPackageName()))
            activity.startActivity(intent)
    except Exception as e:
        core.log_exception(type(e), e, e.__traceback__)


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------

class Card(BoxLayout):
    """Rounded container. Vertical cards grow with their content."""

    def __init__(self, bg=BG_CARD, radius=14, pad=14, spacing=8, auto=True, **kw):
        p = list(pad) if isinstance(pad, (list, tuple)) else [pad] * 4
        if len(p) == 2:
            p = [p[0], p[1], p[0], p[1]]
        kw.setdefault("orientation", "vertical")
        kw["padding"] = [S(x) for x in p]
        kw["spacing"] = S(spacing)
        self._auto = auto and kw["orientation"] == "vertical"
        if self._auto:
            kw["size_hint_y"] = None
        super().__init__(**kw)
        if self._auto:
            self.bind(minimum_height=self.setter("height"))
        if bg:
            with self.canvas.before:
                Color(*C(bg))
                self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[S(radius)])
            self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *_):
        self._rect.pos, self._rect.size = self.pos, self.size


def hrow(h=44, spacing=8, **kw):
    return BoxLayout(orientation="horizontal", size_hint_y=None, height=S(h), spacing=S(spacing), **kw)


class Lbl(Label):
    """Wrapping label that is as tall as its text (or fills its parent with fill=True)."""

    def __init__(self, text="", size=14, color=TEXT, bold=False, align="left", fill=False, **kw):
        self._fill = fill
        if not fill:
            kw.setdefault("size_hint_y", None)
        super().__init__(text=text, font_size=F(size), color=C(color), bold=bold,
                         halign=align, valign="middle", **kw)
        self.bind(size=self._sync, texture_size=self._sync)
        self._sync()

    def _sync(self, *_):
        if self._fill:
            self.text_size = self.size
        else:
            self.text_size = (self.width, None)
            self.height = max(self.texture_size[1], 1) + S(2)


def lighten(rgba, amount=0.12):
    return tuple(min(1, c + amount) for c in rgba[:3]) + (rgba[3],)


class Btn(ButtonBehavior, Label):
    KINDS = {
        "accent": (ACCENT, "#ffffff"),
        "secondary": (BG_CARD_ALT, TEXT),
        "soft": (ACCENT_SOFT, ACCENT),
        "danger": (DANGER_SOFT, DANGER),
        "success": (SUCCESS_SOFT, SUCCESS),
        "warning": (WARNING_SOFT, WARNING),
        "ghost": (None, MUTED),
    }

    def __init__(self, text="", kind="secondary", size=14, h=46, align="center", **kw):
        kw.setdefault("size_hint_y", None)
        kw.setdefault("height", S(h))
        self.kind = kind
        super().__init__(text=text, font_size=F(size), bold=True, halign=align, valign="middle",
                         shorten=True, shorten_from="right", **kw)
        with self.canvas.before:
            self._col = Color(0, 0, 0, 0)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[S(12)])
        self.padding_x = S(12)
        self.bind(pos=self._layout, size=self._layout, state=self._paint, disabled=self._paint)
        self._layout()
        self._paint()

    def _layout(self, *_):
        self._rect.pos, self._rect.size = self.pos, self.size
        self.text_size = (max(self.width - S(24), 1), self.height)

    def set_kind(self, kind):
        self.kind = kind
        self._paint()

    def _paint(self, *_):
        back, fore = self.KINDS[self.kind]
        rgba = C(back) if back else (0, 0, 0, 0)
        if self.state == "down":
            rgba = lighten(rgba, 0.12) if back else (1, 1, 1, 0.06)
        fg = C(fore)
        if self.disabled:
            rgba = rgba[:3] + (rgba[3] * 0.5,)
            fg = fg[:3] + (0.5,)
        self._col.rgba = rgba
        self.color = fg


class TapBox(ButtonBehavior, BoxLayout):
    pass


class Entry(TextInput):
    def __init__(self, hint="", h=48, multiline=False, **kw):
        kw.setdefault("size_hint_y", None)
        super().__init__(
            hint_text=hint, multiline=multiline, height=S(h), write_tab=False,
            background_normal="", background_active="", background_color=(0, 0, 0, 0),
            foreground_color=C(TEXT), hint_text_color=C(MUTED), cursor_color=C(ACCENT),
            selection_color=C(ACCENT) [:3] + (0.35,),
            font_size=F(15), padding=[S(14), S(13), S(14), S(13)], **kw)
        with self.canvas.before:
            self._col = Color(*C(BG_CARD_ALT))
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[S(12)])
        self.bind(pos=self._sync, size=self._sync, focus=self._focus)

    def _sync(self, *_):
        self._rect.pos, self._rect.size = self.pos, self.size

    def _focus(self, _w, focused):
        self._col.rgba = C(BG_HOVER if focused else BG_CARD_ALT)


def field_label(text, hint=None):
    return Lbl(text + (f"   [color={MUTED[1:]}]{hint}[/color]" if hint else ""),
               size=13, color=TEXT_2, bold=True, markup=True)


def set_visible(widget, visible, height=None):
    if visible:
        widget.height = height if height is not None else getattr(widget, "_h", widget.height)
        widget.opacity, widget.disabled = 1, False
    else:
        if widget.height:
            widget._h = widget.height
        widget.height, widget.opacity, widget.disabled = 0, 0, True


def confirm(title, text, on_yes, yes="Delete", kind="danger"):
    view = ModalView(size_hint=(0.9, None), height=S(200), auto_dismiss=True, background="",
                     background_color=(0, 0, 0, 0), overlay_color=(0, 0, 0, 0.65))
    card = Card(bg=BG_CARD, radius=18, pad=18, spacing=12)
    card.add_widget(Lbl(title, size=18, bold=True))
    card.add_widget(Lbl(text, size=14, color=TEXT_2))
    buttons = hrow(48, 10)
    buttons.add_widget(Btn("Cancel", on_release=lambda *_: view.dismiss()))

    def accept(*_):
        view.dismiss()
        on_yes()
    buttons.add_widget(Btn(yes, kind=kind, on_release=accept))
    card.add_widget(buttons)
    card.bind(height=lambda _w, h: setattr(view, "height", h))
    view.add_widget(card)
    view.open()


class PathPicker(ModalView):
    """Full-screen folder / file browser (Android has no usable built-in one)."""

    def __init__(self, on_done, mode="dir", exts=None, multi=False, title="Choose a folder", start=None):
        super().__init__(size_hint=(1, 1), auto_dismiss=False, background="",
                         background_color=(0, 0, 0, 0), overlay_color=(0, 0, 0, 0))
        self.on_done, self.mode, self.multi = on_done, mode, multi
        self.exts = tuple(e.lower() for e in (exts or ()))
        self.selected = []
        fallback = core.STORAGE_ROOT if os.path.isdir(core.STORAGE_ROOT) else os.path.expanduser("~")
        self.path = start if start and os.path.isdir(start) else fallback

        body = Card(bg=BG_APP, radius=0, pad=[12, 12], spacing=8, auto=False)
        body.add_widget(Lbl(title, size=20, bold=True))
        shortcuts = hrow(40, 8)
        for label, path in (("Storage", core.STORAGE_ROOT),
                            ("Download", os.path.join(core.STORAGE_ROOT, "Download")),
                            ("Pictures", os.path.join(core.STORAGE_ROOT, "Pictures"))):
            if os.path.isdir(path):
                shortcuts.add_widget(Btn(label, kind="soft", h=40, size=13,
                                         on_release=lambda _b, p=path: self.go(p)))
        body.add_widget(shortcuts)
        self.path_lbl = Lbl("", size=12, color=MUTED)
        body.add_widget(self.path_lbl)

        scroll = ScrollView(bar_width=S(3), do_scroll_x=False)
        self.list = BoxLayout(orientation="vertical", size_hint_y=None, spacing=S(6))
        self.list.bind(minimum_height=self.list.setter("height"))
        scroll.add_widget(self.list)
        body.add_widget(scroll)

        footer = hrow(50, 10)
        footer.add_widget(Btn("Cancel", on_release=lambda *_: self.dismiss()))
        self.ok_btn = Btn("", kind="accent", on_release=self._finish)
        footer.add_widget(self.ok_btn)
        body.add_widget(footer)
        self.add_widget(body)
        self.populate()

    def go(self, path):
        self.path = path
        self.populate()

    def populate(self):
        self.list.clear_widgets()
        self.path_lbl.text = self.path
        if self.path.rstrip("/") not in ("", "/storage/emulated"):
            parent = os.path.dirname(self.path.rstrip("/")) or "/"
            self.list.add_widget(Btn(".. (up)", kind="soft", align="left",
                                     on_release=lambda *_: self.go(parent)))
        try:
            entries = []
            with os.scandir(self.path) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        entries.append((e.is_dir(), e.name, e.path))
                    except OSError:
                        pass
            entries.sort(key=lambda t: (not t[0], t[1].lower()))
        except OSError:
            self.list.add_widget(Lbl("Can't read this folder. Allow storage access in Settings.",
                                     color=WARNING))
            entries = []
        shown = 0
        for is_dir, name, path in entries:
            if is_dir:
                self.list.add_widget(Btn(name + "/", align="left",
                                         on_release=lambda _b, p=path: self.go(p)))
                shown += 1
            elif self.mode == "files" and (not self.exts or name.lower().endswith(self.exts)):
                chosen = path in self.selected
                self.list.add_widget(Btn(name, kind="success" if chosen else "ghost", align="left",
                                         on_release=lambda b, p=path: self._toggle(b, p)))
                shown += 1
        if not shown:
            self.list.add_widget(Lbl("Nothing to show here.", color=MUTED))
        self._update_ok()

    def _toggle(self, btn, path):
        if path in self.selected:
            self.selected.remove(path)
            btn.set_kind("ghost")
        else:
            if not self.multi:
                self.selected = []
            self.selected.append(path)
            if not self.multi:
                self.populate()
                return
            btn.set_kind("success")
        self._update_ok()

    def _update_ok(self):
        if self.mode == "dir":
            self.ok_btn.text = "Select this folder"
            self.ok_btn.disabled = False
        else:
            n = len(self.selected)
            self.ok_btn.text = f"Add {n} file{'s' if n != 1 else ''}" if n else "Pick a file"
            self.ok_btn.disabled = n == 0

    def _finish(self, *_):
        self.dismiss()
        self.on_done(self.path if self.mode == "dir" else list(self.selected))


# ---------------------------------------------------------------------------
# Page scaffolding
# ---------------------------------------------------------------------------

MAIN_PAGES = ("browse", "library", "export", "settings")


class Page(Screen):
    def __init__(self, app, name, **kw):
        super().__init__(name=name, **kw)
        self.app = app

    def on_pre_enter(self, *_):
        self.refresh()

    def refresh(self):
        pass

    def scaffold(self, title, back=None, right=None):
        """Header + scrolling body. Returns the body (a vertical, auto-height box)."""
        root = BoxLayout(orientation="vertical")
        head = hrow(58, 6, padding=[S(10), S(8), S(10), 0])
        if back:
            head.add_widget(Btn("<", kind="ghost", size=20, size_hint_x=None, width=S(44), on_release=back))
        self.title_lbl = Lbl(title, size=22, bold=True, fill=True)
        head.add_widget(self.title_lbl)
        if right is not None:
            head.add_widget(right)
        root.add_widget(head)
        self.scroll = ScrollView(bar_width=S(3), do_scroll_x=False)
        body = Card(bg=None, pad=[14, 6, 14, 18], spacing=12)
        self.scroll.add_widget(body)
        root.add_widget(self.scroll)
        self.add_widget(root)
        return body

    def empty(self, parent, title, subtitle=""):
        card = Card(bg=BG_CARD, radius=16, pad=22, spacing=6)
        card.add_widget(Lbl(title, size=17, bold=True, align="center"))
        if subtitle:
            card.add_widget(Lbl(subtitle, size=13, color=MUTED, align="center"))
        parent.add_widget(card)


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------

def make_cover(source, ratio=9 / 16):
    img = AsyncImage(source=source or "", fit_mode="cover", size_hint_y=None)
    img.bind(width=lambda w, v: setattr(w, "height", v * ratio))
    return img


class BrowsePage(Page):
    def __init__(self, app):
        super().__init__(app, "browse")
        refresh_btn = Btn("Refresh", kind="soft", size=13, h=40, size_hint_x=None, width=S(86),
                          on_release=lambda *_: app.load_catalog())
        body = self.scaffold("Browse", right=refresh_btn)
        self.search = Entry("Search plugins...")
        self.search.bind(text=self._on_search)
        body.add_widget(self.search)
        self.status = Lbl("", size=12, color=MUTED)
        body.add_widget(self.status)
        self.grid = Card(bg=None, pad=0, spacing=14)
        body.add_widget(self.grid)
        self.buttons = {}
        self._debounce = None

    def _on_search(self, *_):
        if self._debounce:
            self._debounce.cancel()
        self._debounce = Clock.schedule_once(lambda dt: self.rebuild(), 0.3)

    def refresh(self):
        self.rebuild()

    def visible_plugins(self):
        query = self.search.text.strip().lower()
        plugins = self.app.plugins
        if query:
            plugins = [p for p in plugins if query in p["name"].lower()
                       or query in p["short_description"].lower() or query in p["description"].lower()]
        return plugins

    def update_status(self):
        app = self.app
        if app.loading:
            self.status.text = "Loading the catalog..."
            self.status.color = C(MUTED)
        elif app.catalog_error and app.plugins:
            self.status.text = "Offline - showing the saved catalog."
            self.status.color = C(WARNING)
        elif app.catalog_error:
            self.status.text = app.catalog_error
            self.status.color = C(DANGER)
        else:
            n = len(app.plugins)
            when = time_ago(app.catalog_time)
            self.status.text = f"{n} plugin{'s' if n != 1 else ''}" + (f" · updated {when}" if when else "")
            self.status.color = C(MUTED)

    def rebuild(self):
        self.update_status()
        self.grid.clear_widgets()
        self.buttons = {}
        plugins = self.visible_plugins()
        if not plugins:
            if not self.app.loading:
                self.empty(self.grid, "No plugins found",
                           "Try another search." if self.search.text.strip() else "Pull to refresh when you're online.")
            return
        for plugin in plugins:
            self.grid.add_widget(self.make_tile(plugin))

    def make_tile(self, plugin):
        card = Card(bg=BG_CARD, radius=16, pad=0, spacing=0)
        tap = TapBox(orientation="vertical", size_hint_y=None)
        tap.bind(minimum_height=tap.setter("height"))
        tap.add_widget(make_cover(plugin["thumb"]))
        info = Card(bg=None, pad=[14, 12, 14, 4], spacing=4)
        info.add_widget(Lbl(plugin["name"], size=17, bold=True))
        if plugin["short_description"]:
            info.add_widget(Lbl(core.ellipsize(plugin["short_description"], 120), size=13, color=TEXT_2))
        tap.add_widget(info)
        tap.bind(on_release=lambda *_: self.app.open_plugin(plugin, "browse"))
        card.add_widget(tap)
        action = Card(bg=None, pad=[14, 6, 14, 14], spacing=0)
        btn = Btn("", on_release=lambda *_: self.app.on_action(plugin))
        action.add_widget(btn)
        card.add_widget(action)
        self.buttons[plugin["id"]] = (btn, plugin)
        self.app.style_action(btn, plugin)
        return card

    def refresh_buttons(self):
        for btn, plugin in self.buttons.values():
            self.app.style_action(btn, plugin)


# ---------------------------------------------------------------------------
# Plugin detail
# ---------------------------------------------------------------------------

class PluginPage(Page):
    def __init__(self, app):
        super().__init__(app, "plugin")
        self.plugin = None
        self.back_to = "browse"
        self.body = self.scaffold("Plugin", back=lambda *_: app.back())
        self.action_btn = None

    def show(self, plugin, back_to):
        self.plugin, self.back_to = plugin, back_to
        self.build()

    def build(self):
        plugin, body = self.plugin, self.body
        body.clear_widgets()
        self.title_lbl.text = "Plugin"
        self.scroll.scroll_y = 1
        if not plugin:
            return
        images = plugin["images"]
        if images:
            carousel = Carousel(direction="right", size_hint_y=None, loop=len(images) > 1)
            carousel.bind(width=lambda w, v: setattr(w, "height", v * 9 / 16))
            for url in images:
                carousel.add_widget(AsyncImage(source=url, fit_mode="contain"))
            body.add_widget(carousel)
            if len(images) > 1:
                counter = Lbl(f"1 / {len(images)}  ·  swipe for more", size=12, color=MUTED, align="center")
                carousel.bind(index=lambda _c, i: setattr(counter, "text", f"{i + 1} / {len(images)}  ·  swipe for more"))
                body.add_widget(counter)
        body.add_widget(Lbl(plugin["name"], size=24, bold=True))
        meta = [f"Version {plugin['revision']}"]
        if core.format_date(plugin["updated"]):
            meta.append("updated " + core.format_date(plugin["updated"]))
        body.add_widget(Lbl("  ·  ".join(meta), size=12, color=MUTED))
        self.action_btn = Btn("", h=52, size=16, on_release=lambda *_: self.app.on_action(plugin))
        body.add_widget(self.action_btn)
        self.remove_btn = Btn("Remove from library", kind="danger", on_release=lambda *_: self.app.uninstall_dialog(plugin))
        body.add_widget(self.remove_btn)
        text = plugin["description"] or plugin["short_description"] or "No description."
        card = Card(bg=BG_CARD, radius=16, pad=16)
        card.add_widget(Lbl("About", size=15, bold=True))
        card.add_widget(Lbl(text, size=14, color=TEXT_2))
        body.add_widget(card)
        self.refresh_buttons()

    def refresh_buttons(self):
        if not self.plugin or not self.action_btn:
            return
        self.app.style_action(self.action_btn, self.plugin)
        installed = self.app.plugin_state(self.plugin) in ("installed", "update")
        set_visible(self.remove_btn, installed, S(46))


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

class LibraryPage(Page):
    def __init__(self, app):
        super().__init__(app, "library")
        self.body = self.scaffold("Library")

    def refresh(self):
        app, body = self.app, self.body
        app.registry = core.load_installed()
        items = core.scan_plugins_folder()
        by_file = {e.get("filename", "").lower(): (pid, e) for pid, e in app.registry.items() if e.get("filename")}
        catalog = {p["id"]: p for p in app.plugins}
        body.clear_widgets()

        head = Card(bg=BG_CARD, radius=16, pad=14, spacing=4)
        head.add_widget(Lbl(f"{len(items)} plugin{'s' if len(items) != 1 else ''} installed", size=16, bold=True))
        head.add_widget(Lbl(core.PLUGINS_DIR, size=12, color=MUTED))
        updates = [catalog[pid] for pid, _e in by_file.values()
                   if pid in catalog and app.plugin_state(catalog[pid]) == "update"]
        if updates:
            head.add_widget(Btn(f"Update all ({len(updates)})", kind="warning",
                                on_release=lambda *_: [app.start_download(p) for p in updates]))
        body.add_widget(head)

        if not items:
            self.empty(body, "Your library is empty",
                       "Plugins you download from Browse show up here.")
            return
        for item in items:
            body.add_widget(self.make_row(item, by_file.get(item["filename"].lower()), catalog))

    def make_row(self, item, registered, catalog):
        app = self.app
        card = Card(bg=BG_CARD, radius=16, pad=12, spacing=10)
        top = hrow(72, 12)
        pid, entry = registered if registered else (None, None)
        plugin = catalog.get(pid) if pid else None
        thumb = (plugin or {}).get("thumb") or (entry or {}).get("thumb", "")
        if thumb:
            top.add_widget(AsyncImage(source=thumb, fit_mode="cover", size_hint=(None, 1), width=S(112)))
        else:
            top.add_widget(Card(bg=BG_CARD_ALT, radius=10, pad=0, auto=False, size_hint=(None, 1), width=S(112)))
        text = Card(bg=None, pad=0, spacing=2, auto=False)
        name = (plugin or {}).get("name") or (entry or {}).get("name") or os.path.splitext(item["filename"])[0]
        text.add_widget(Lbl(name, size=16, bold=True, fill=True))
        meta = core.format_size(item["size"]) + "  ·  " + core.format_timestamp(item["mtime"])
        text.add_widget(Lbl(meta, size=11, color=MUTED, fill=True))
        top.add_widget(text)
        card.add_widget(top)

        short = (plugin or {}).get("short_description") or (entry or {}).get("short_description", "")
        if short:
            card.add_widget(Lbl(core.ellipsize(short, 140), size=13, color=TEXT_2))
        buttons = hrow(44, 8)
        if plugin:
            buttons.add_widget(Btn("Details", kind="soft", size=13, h=44,
                                   on_release=lambda *_: app.open_plugin(plugin, "library")))
            if app.plugin_state(plugin) == "update":
                buttons.add_widget(Btn("Update", kind="warning", size=13, h=44,
                                       on_release=lambda *_: app.start_download(plugin)))
        buttons.add_widget(Btn("Delete", kind="danger", size=13, h=44,
                               on_release=lambda *_: confirm(
                                   "Delete plugin", f"Delete \"{name}\" from your plugins folder?",
                                   lambda: self.delete(item, pid))))
        card.add_widget(buttons)
        return card

    def delete(self, item, pid):
        try:
            os.remove(item["path"])
        except OSError as e:
            self.app.toast(f"Couldn't delete: {e}", "error")
            return
        if pid and pid in self.app.registry:
            self.app.registry.pop(pid, None)
            core.save_installed(self.app.registry)
        self.app.toast("Plugin deleted.", "success")
        self.refresh()
        self.app.refresh_buttons()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class ExportPage(Page):
    def __init__(self, app):
        super().__init__(app, "export")
        self.body = self.scaffold("Export")
        self.running = False
        self.log_lines = []
        self.build()

    def build(self):
        app, body = self.app, self.body
        body.clear_widgets()
        self.sources_card = Card(bg=BG_CARD, radius=16, pad=16, spacing=10)
        body.add_widget(self.sources_card)

        dest = Card(bg=BG_CARD, radius=16, pad=16, spacing=6)
        dest.add_widget(Lbl("Export to", size=15, bold=True))
        dest.add_widget(Lbl(core.EXPORT_DIR, size=13, color=TEXT_2))
        dest.add_widget(Lbl("This folder is created automatically on the first export.", size=12, color=MUTED))
        body.add_widget(dest)

        self.export_btn = Btn("Export plugins", kind="accent", h=54, size=16, on_release=lambda *_: self.start())
        body.add_widget(self.export_btn)
        self.progress = ProgressBar(max=100, value=0, size_hint_y=None, height=S(6))
        body.add_widget(self.progress)
        self.log_slot = Card(bg=None, pad=0, spacing=0)
        body.add_widget(self.log_slot)
        self.log_card = Card(bg=BG_CARD, radius=16, pad=14)
        self.log_lbl = Lbl("", size=12, color=TEXT_2)
        self.log_card.add_widget(self.log_lbl)

        self.exported_card = Card(bg=BG_CARD, radius=16, pad=16, spacing=8)
        body.add_widget(self.exported_card)

    def refresh(self):
        self.render_sources()
        self.render_exported()

    def render_sources(self):
        card = self.sources_card
        card.clear_widgets()
        card.add_widget(Lbl("Source folders", size=15, bold=True))
        card.add_widget(Lbl("Pick the folders that contain your plugins. Plugin ID folders and loose "
                            ".rbxm files are both found, including inside sub-folders.", size=12, color=MUTED))
        sources = self.app.settings.get("sources", [])
        if not sources:
            card.add_widget(Lbl("No folders yet.", size=13, color=WARNING))
        for path in sources:
            line = hrow(44, 8)
            line.add_widget(Lbl(path, size=13, color=TEXT_2, fill=True))
            line.add_widget(Btn("Remove", kind="danger", size=12, h=40, size_hint_x=None, width=S(84),
                                on_release=lambda _b, p=path: self.remove_source(p)))
            card.add_widget(line)
        card.add_widget(Btn("Add folder", kind="soft", on_release=lambda *_: self.add_source()))

    def add_source(self):
        def done(path):
            sources = self.app.settings.setdefault("sources", [])
            if path not in sources:
                sources.append(path)
                self.app.save_settings()
            self.render_sources()
        PathPicker(done, mode="dir", title="Choose a source folder").open()

    def remove_source(self, path):
        sources = self.app.settings.get("sources", [])
        if path in sources:
            sources.remove(path)
            self.app.save_settings()
        self.render_sources()

    def render_exported(self):
        card = self.exported_card
        card.clear_widgets()
        entries = sorted(core.exported_entries().items(), key=lambda kv: kv[1]["filename"].lower())
        card.add_widget(Lbl(f"Exported plugins ({len(entries)})", size=15, bold=True))
        if not entries:
            card.add_widget(Lbl("Nothing exported yet.", size=13, color=MUTED))
        for pid, meta in entries[:150]:
            line = hrow(40, 8)
            line.add_widget(Lbl(os.path.splitext(meta["filename"])[0], size=13, color=TEXT_2, fill=True))
            line.add_widget(Btn("Remove", kind="ghost", size=12, h=36, size_hint_x=None, width=S(76),
                                on_release=lambda _b, i=pid, m=meta: confirm(
                                    "Remove export", f"Delete \"{m['filename']}\" from the export folder?",
                                    lambda: self.remove_exported(i, m))))
            card.add_widget(line)
        if len(entries) > 150:
            card.add_widget(Lbl(f"...and {len(entries) - 150} more", size=12, color=MUTED))

    def remove_exported(self, pid, meta):
        try:
            os.remove(os.path.join(core.EXPORT_DIR, meta["filename"]))
        except OSError:
            pass
        index = core.load_index()
        index.pop(pid, None)
        core.save_index(index)
        self.render_exported()

    # -- running an export
    def log(self, message):
        self.log_lines.extend(str(message).split("\n"))
        self.log_lines = self.log_lines[-60:]
        self.log_lbl.text = "\n".join(self.log_lines)

    def start(self):
        if self.running:
            return
        sources = list(self.app.settings.get("sources", []))
        if not sources:
            self.app.toast("Add at least one source folder first.", "warning")
            return
        if not has_storage_access():
            self.app.toast("Allow storage access in Settings first.", "warning")
            return
        self.running = True
        self.log_lines = []
        self.log_lbl.text = ""
        if self.log_card.parent is None:
            self.log_slot.add_widget(self.log_card)
        self.progress.value = 0
        self.export_btn.text, self.export_btn.disabled = "Exporting...", True

        def work():
            result = core.export_plugins(
                sources, log=lambda m: ui(self.log, m),
                progress_callback=lambda d, t: ui(setattr, self.progress, "value", d * 100 / max(t, 1)))
            ui(self.finish, result)
        bg(work)

    def finish(self, result):
        self.running = False
        self.export_btn.text, self.export_btn.disabled = "Export plugins", False
        self.progress.value = 100 if result else 0
        if result:
            copied, updated = result
            self.app.toast(f"Exported: {copied} new, {updated} updated.", "success")
        else:
            self.app.toast("Export didn't finish - see the log.", "error")
        self.render_exported()


# ---------------------------------------------------------------------------
# Settings (+ admin sign-in)
# ---------------------------------------------------------------------------

class SettingsPage(Page):
    def __init__(self, app):
        super().__init__(app, "settings")
        self.body = self.scaffold("Settings")
        self.storage_card = Card(bg=BG_CARD, radius=16, pad=16, spacing=8)
        self.ui_card = Card(bg=BG_CARD, radius=16, pad=16, spacing=10)
        self.admin_card = Card(bg=BG_CARD, radius=16, pad=16, spacing=8)
        self.info_card = Card(bg=BG_CARD, radius=16, pad=16, spacing=8)
        for card in (self.ui_card, self.storage_card, self.admin_card, self.info_card):
            self.body.add_widget(card)
        self.render_ui()

    def refresh(self):
        self.render_storage()
        self.render_admin()
        self.render_info()

    # -- storage
    def render_storage(self):
        card = self.storage_card
        card.clear_widgets()
        card.add_widget(Lbl("Storage", size=15, bold=True))
        if has_storage_access():
            card.add_widget(Lbl("Access to internal storage is allowed.", size=13, color=SUCCESS))
        else:
            card.add_widget(Lbl("RobloxTools needs \"All files access\" to read your source folders and "
                                "write to the plugins and export folders.", size=13, color=WARNING))
            card.add_widget(Btn("Grant storage access", kind="accent",
                                on_release=lambda *_: request_storage_access()))
        card.add_widget(Lbl(f"Store plugins:  {core.PLUGINS_DIR}", size=12, color=MUTED))
        card.add_widget(Lbl(f"Exports:  {core.EXPORT_DIR}", size=12, color=MUTED))

    # -- interface
    def render_ui(self):
        card = self.ui_card
        card.clear_widgets()
        card.add_widget(Lbl("Interface", size=15, bold=True))
        card.add_widget(self.stepper("Interface size", "ui_scale", "Buttons, cards and spacing"))
        card.add_widget(self.stepper("Text size", "text_scale", "Letters only"))
        card.add_widget(Btn("Reset to 100%", kind="ghost", h=40, size=13, on_release=lambda *_: self.reset_scale()))

    def stepper(self, title, key, hint):
        value = float(self.app.settings.get(key, 1.0))
        box = Card(bg=None, pad=0, spacing=4)
        box.add_widget(Lbl(title, size=14, color=TEXT_2, bold=True))
        line = hrow(46, 10)
        line.add_widget(Btn("-", kind="secondary", size=20, size_hint_x=None, width=S(56),
                            on_release=lambda *_: self.change_scale(key, -0.1)))
        line.add_widget(Lbl(f"{round(value * 100)}%", size=17, bold=True, align="center", fill=True))
        line.add_widget(Btn("+", kind="secondary", size=20, size_hint_x=None, width=S(56),
                            on_release=lambda *_: self.change_scale(key, 0.1)))
        box.add_widget(line)
        box.add_widget(Lbl(hint, size=11, color=MUTED))
        return box

    def change_scale(self, key, delta):
        value = round(min(1.5, max(0.7, float(self.app.settings.get(key, 1.0)) + delta)), 2)
        self.app.settings[key] = value
        self.app.save_settings()
        self.app.apply_scale("settings")

    def reset_scale(self):
        self.app.settings["ui_scale"] = self.app.settings["text_scale"] = 1.0
        self.app.save_settings()
        self.app.apply_scale("settings")

    # -- admin
    def render_admin(self):
        app, card = self.app, self.admin_card
        card.clear_widgets()
        card.add_widget(Lbl("Catalog admin", size=15, bold=True))
        if app.api.signed_in:
            card.add_widget(Lbl(f"Signed in as {app.api.email}", size=13, color=SUCCESS))
            card.add_widget(Btn("Open catalog manager", kind="accent",
                                on_release=lambda *_: app.go("admin")))
            card.add_widget(Btn("Sign out", kind="secondary", on_release=lambda *_: app.sign_out()))
            return
        card.add_widget(Lbl("Only for people who manage the plugin catalog.", size=12, color=MUTED))
        self.email = Entry("Admin email", input_type="mail")
        self.password = Entry("Password", password=True)
        self.login_status = Lbl("", size=12, color=MUTED)
        self.login_btn = Btn("Sign in", kind="accent", on_release=lambda *_: self.sign_in())
        self.password.bind(on_text_validate=lambda *_: self.sign_in())
        for w in (self.email, self.password, self.login_btn, self.login_status):
            card.add_widget(w)

    def sign_in(self):
        app = self.app
        if self.login_btn.disabled:
            return
        email, password = self.email.text.strip(), self.password.text
        if not email or not password:
            self.login_status.text, self.login_status.color = "Enter your admin email and password.", C(WARNING)
            return
        now = time.time()
        app.login_failures = [t for t in app.login_failures if now - t < 120]
        if len(app.login_failures) >= 5:
            wait = int(120 - (now - app.login_failures[0]))
            self.login_status.text, self.login_status.color = f"Too many attempts - try again in {wait} s.", C(DANGER)
            return
        self.login_btn.text, self.login_btn.disabled = "Signing in...", True
        self.login_status.text = ""

        def work():
            try:
                app.api.sign_in(email, password)
                ui(self.signed_in, None)
            except ApiError as e:
                ui(self.signed_in, str(e))
        bg(work)

    def signed_in(self, error):
        app = self.app
        if error:
            app.login_failures.append(time.time())
            self.login_btn.text, self.login_btn.disabled = "Sign in", False
            if "invalid" in error.lower() and "credential" in error.lower():
                error = "Wrong email or password."
            self.login_status.text, self.login_status.color = error, C(DANGER)
            return
        app.login_failures = []
        self.password.text = ""
        app.admin_loaded = False
        app.toast(f"Signed in as {app.api.email}", "success")
        self.render_admin()
        app.go("admin")

    # -- info
    def render_info(self):
        app, card = self.app, self.info_card
        card.clear_widgets()
        card.add_widget(Lbl("About", size=15, bold=True))
        when = time_ago(app.catalog_time)
        card.add_widget(Lbl(f"RobloxTools {core.APP_VERSION} for Android", size=13, color=TEXT_2))
        card.add_widget(Lbl(f"Catalog: {len(app.plugins)} plugins" + (f", updated {when}" if when else ""),
                            size=12, color=MUTED))
        card.add_widget(Lbl(f"Cached data: {core.format_size(core.cache_size_bytes())}", size=12, color=MUTED))
        card.add_widget(Btn("Clear cache", kind="secondary", on_release=lambda *_: self.clear_cache()))

    def clear_cache(self):
        core.clear_cache()
        self.app.toast("Cache cleared.", "success")
        self.render_info()


# ---------------------------------------------------------------------------
# Catalog manager (admin): list + phone-sized editor
# ---------------------------------------------------------------------------

class AdminPage(Page):
    def __init__(self, app):
        super().__init__(app, "admin")
        new_btn = Btn("New", kind="accent", size=13, h=40, size_hint_x=None, width=S(72),
                      on_release=lambda *_: app.edit_plugin(None))
        self.body = self.scaffold("Catalog", back=lambda *_: app.back(), right=new_btn)
        self.status = Lbl("", size=12, color=MUTED)

    def refresh(self):
        app = self.app
        if not app.api.signed_in:
            app.go("settings")
            return
        if not app.admin_loaded:
            app.load_admin_rows()
        self.render()

    def render(self):
        app, body = self.app, self.body
        body.clear_widgets()
        body.add_widget(self.status)
        if app.admin_loading:
            self.status.text = "Loading..."
        else:
            n = len(app.admin_rows)
            self.status.text = f"{n} plugin{'s' if n != 1 else ''} · tap one to edit"
        for plugin in app.admin_rows:
            card = Card(bg=BG_CARD, radius=14, pad=0, spacing=0)
            tap = TapBox(orientation="vertical", size_hint_y=None, padding=[S(14), S(12)], spacing=S(2))
            tap.bind(minimum_height=tap.setter("height"))
            tap.add_widget(Lbl(plugin["name"], size=16, bold=True))
            tag = f"{plugin['id']}  ·  v{plugin['revision']}" + ("" if plugin["published"] else "  ·  HIDDEN")
            tap.add_widget(Lbl(tag, size=12, color=MUTED if plugin["published"] else WARNING))
            tap.bind(on_release=lambda _t, p=plugin: app.edit_plugin(p))
            card.add_widget(tap)
            body.add_widget(card)


class EditorPage(Page):
    def __init__(self, app):
        super().__init__(app, "editor")
        self.original, self.images = None, []
        self.dirty = self.busy = self.id_touched = self._loading = self._auto = False

        root = BoxLayout(orientation="vertical")
        head = hrow(58, 6, padding=[S(10), S(8), S(10), 0])
        head.add_widget(Btn("<", kind="ghost", size=20, size_hint_x=None, width=S(44),
                            on_release=lambda *_: self.request_leave()))
        self.title_lbl = Lbl("Edit plugin", size=22, bold=True, fill=True)
        head.add_widget(self.title_lbl)
        root.add_widget(head)

        self.scroll = ScrollView(bar_width=S(3), do_scroll_x=False)
        form = Card(bg=None, pad=[14, 6, 14, 24], spacing=8)
        self.scroll.add_widget(form)
        root.add_widget(self.scroll)
        self.build_form(form)

        footer = Card(bg=BG_SIDEBAR, radius=0, pad=[12, 10], spacing=6)
        self.status = Lbl("", size=12, color=MUTED)
        footer.add_widget(self.status)
        buttons = hrow(50, 10)
        self.save_btn = Btn("Save", kind="accent", h=50, on_release=lambda *_: self.save())
        self.delete_btn = Btn("Delete", kind="danger", h=50, size_hint_x=None, width=S(100),
                              on_release=lambda *_: self.delete())
        buttons.add_widget(self.save_btn)
        buttons.add_widget(self.delete_btn)
        footer.add_widget(buttons)
        root.add_widget(footer)
        self.add_widget(root)

    def build_form(self, form):
        def section(title):
            form.add_widget(Lbl(title, size=17, bold=True, color=ACCENT))

        section("Details")
        form.add_widget(field_label("Name", "required"))
        self.f_name = Entry("e.g. Building Tools Pro")
        self.f_name.bind(text=self.on_name)
        form.add_widget(self.f_name)

        form.add_widget(field_label("Plugin ID", "a-z, 0-9, dashes · permanent"))
        self.f_id = Entry("building-tools-pro", input_type="text")
        self.f_id.bind(text=self.on_id)
        form.add_widget(self.f_id)

        form.add_widget(field_label("Short description", "shown on the tile"))
        self.f_short = Entry("One sentence about what it does")
        self.f_short.bind(text=self.on_short)
        form.add_widget(self.f_short)
        self.short_count = Lbl("0 / 120", size=11, color=MUTED, align="right")
        form.add_widget(self.short_count)

        form.add_widget(field_label("Full description", "shown on the plugin's page"))
        self.f_desc = Entry("", multiline=True, h=170)
        self.f_desc.bind(text=self.mark)
        form.add_widget(self.f_desc)

        section("Download")
        form.add_widget(field_label("Download link", "direct link to the .rbxm"))
        self.f_url = Entry("https://.../plugin.rbxm", input_type="url")
        self.f_url.bind(text=self.on_url)
        form.add_widget(self.f_url)
        self.test_btn = Btn("Test link", kind="soft", h=44, on_release=lambda *_: self.test_link())
        form.add_widget(self.test_btn)
        self.test_lbl = Lbl("", size=12, color=MUTED)
        form.add_widget(self.test_lbl)
        form.add_widget(field_label("File name", "optional · used in the plugins folder"))
        self.f_filename = Entry("Leave empty to use the link's file name")
        self.f_filename.bind(text=self.mark)
        form.add_widget(self.f_filename)

        section("Images")
        form.add_widget(Lbl("The first image is the cover. 16:9 looks best.", size=12, color=MUTED))
        self.images_box = Card(bg=None, pad=0, spacing=8)
        form.add_widget(self.images_box)
        form.add_widget(Btn("Add images", kind="soft", on_release=lambda *_: self.add_images()))

        section("Publishing")
        pub = hrow(44, 12)
        pub.add_widget(Lbl("Visible to everyone in Browse", size=14, color=TEXT_2, fill=True))
        self.f_published = Switch(active=True, size_hint=(None, None), size=(S(83), S(32)))
        self.f_published.bind(active=self.mark)
        pub.add_widget(self.f_published)
        form.add_widget(pub)

        self.bump_row = hrow(52, 10)
        self.f_bump = CheckBox(size_hint=(None, None), size=(S(36), S(36)), color=C(ACCENT))
        self.f_bump.bind(active=self.mark)
        self.bump_row.add_widget(self.f_bump)
        self.bump_row.add_widget(Lbl("Release as a new version (users see \"Update\")", size=13, color=TEXT_2, fill=True))
        form.add_widget(self.bump_row)
        self.revision_lbl = Lbl("", size=12, color=MUTED)
        form.add_widget(self.revision_lbl)

    # -- loading
    def open(self, plugin):
        self._loading = True
        self.original = plugin
        p = plugin or {}
        self.title_lbl.text = "Edit plugin" if plugin else "New plugin"
        self.f_name.text = p.get("name", "")
        self.f_id.text = p.get("id", "")
        self.f_id.readonly = bool(plugin)
        self.f_id.foreground_color = C(MUTED if plugin else TEXT)
        self.f_short.text = p.get("short_description", "")
        self.f_desc.text = p.get("description", "")
        self.f_url.text = p.get("download_url", "")
        self.f_filename.text = p.get("filename", "")
        self.f_published.active = p.get("published", True)
        self.f_bump.active = False
        self.images = [{"url": u} for u in p.get("images", [])]
        self.id_touched = bool(plugin)
        self.test_lbl.text = self.status.text = ""
        self.short_count.text = f"{len(self.f_short.text)} / 120"
        set_visible(self.bump_row, bool(plugin), S(52))
        self.delete_btn.width = S(100) if plugin else 0
        set_visible(self.delete_btn, bool(plugin), S(50))
        self.revision_lbl.text = (f"Current version: {p.get('revision', 1)}" if plugin else "")
        self._loading = False
        self.render_images()
        self.set_dirty(False)
        self.scroll.scroll_y = 1

    def on_pre_enter(self, *_):
        pass

    # -- change tracking
    def set_dirty(self, dirty):
        self.dirty = dirty
        self.save_btn.text = "Save changes" if dirty else "Save"

    def mark(self, *_):
        if not self._loading:
            self.set_dirty(True)

    def on_name(self, _w, value):
        if not self._loading and self.original is None and not self.id_touched:
            self._auto = True
            self.f_id.text = core.slugify(value)
            self._auto = False
        self.mark()

    def on_id(self, _w, value):
        if not self._loading and not self._auto:
            self.id_touched = True
        self.mark()

    def on_short(self, _w, value):
        self.short_count.text = f"{len(value)} / 120"
        self.short_count.color = C(DANGER if len(value) > 120 else MUTED)
        self.mark()

    def on_url(self, *_):
        self.test_lbl.text = ""
        self.mark()

    # -- link test
    def test_link(self):
        url = self.f_url.text.strip()
        if not url:
            self.test_lbl.text, self.test_lbl.color = "Paste a link first.", C(WARNING)
            return
        self.test_btn.disabled = True
        self.test_lbl.text, self.test_lbl.color = "Checking...", C(MUTED)

        def work():
            ok, message = core.check_download_link(url)
            ui(self.link_tested, ok, message)
        bg(work)

    def link_tested(self, ok, message):
        self.test_btn.disabled = False
        self.test_lbl.text, self.test_lbl.color = message, C(SUCCESS if ok else DANGER)

    # -- images
    def add_images(self):
        def done(paths):
            for path in paths:
                self.images.append({"path": path})
            self.render_images()
            self.mark()
        PathPicker(done, mode="files", exts=core.IMAGE_EXTENSIONS, multi=True, title="Pick images").open()

    def move_image(self, index, delta):
        target = index + delta
        if 0 <= target < len(self.images):
            self.images[index], self.images[target] = self.images[target], self.images[index]
            self.render_images()
            self.mark()

    def remove_image(self, index):
        self.images.pop(index)
        self.render_images()
        self.mark()

    def render_images(self):
        box = self.images_box
        box.clear_widgets()
        if not self.images:
            box.add_widget(Lbl("No images yet.", size=13, color=MUTED))
        for i, image in enumerate(self.images):
            card = Card(bg=BG_CARD, radius=12, pad=8, spacing=8)
            top = hrow(60, 10)
            top.add_widget(AsyncImage(source=image.get("url") or image["path"], fit_mode="cover",
                                      size_hint=(None, 1), width=S(106)))
            label = "Cover" if i == 0 else f"Image {i + 1}"
            top.add_widget(Lbl(label + ("" if "url" in image else "  ·  new"), size=14, bold=True, fill=True))
            card.add_widget(top)
            ctl = hrow(40, 6)
            ctl.add_widget(Btn("Up", kind="secondary", size=12, h=40, disabled=i == 0,
                               on_release=lambda _b, n=i: self.move_image(n, -1)))
            ctl.add_widget(Btn("Down", kind="secondary", size=12, h=40, disabled=i == len(self.images) - 1,
                               on_release=lambda _b, n=i: self.move_image(n, 1)))
            ctl.add_widget(Btn("Remove", kind="danger", size=12, h=40,
                               on_release=lambda _b, n=i: self.remove_image(n)))
            card.add_widget(ctl)
            box.add_widget(card)

    # -- save / delete
    def values(self):
        return {
            "id": self.f_id.text.strip(),
            "name": self.f_name.text.strip(),
            "short_description": self.f_short.text.strip(),
            "description": self.f_desc.text.strip(),
            "download_url": self.f_url.text.strip(),
            "filename": self.f_filename.text.strip(),
            "published": bool(self.f_published.active),
        }

    def validate(self, v):
        if not v["name"]:
            return "Give the plugin a name."
        if not core.PLUGIN_ID_PATTERN.match(v["id"]):
            return "The ID may only use lowercase letters, numbers and dashes."
        if self.original is None and any(p["id"] == v["id"] for p in self.app.admin_rows):
            return f"A plugin with the ID '{v['id']}' already exists."
        if len(v["short_description"]) > 120:
            return "The short description is longer than 120 characters."
        if not re.match(r"^https?://\S+$", v["download_url"]):
            return "The download link must be a full http(s):// link."
        if v["filename"] and not v["filename"].lower().endswith((".rbxm", ".rbxmx")):
            return "The file name must end with .rbxm or .rbxmx."
        return None

    def set_busy(self, busy, text=""):
        self.busy = busy
        self.save_btn.disabled = self.delete_btn.disabled = busy
        self.status.text, self.status.color = text, C(MUTED)

    def save(self):
        app = self.app
        if self.busy or not app.api.signed_in:
            return
        values = self.values()
        problem = self.validate(values)
        if problem:
            self.status.text, self.status.color = problem, C(DANGER)
            return
        original = self.original
        revision = (original or {}).get("revision", 1)
        if original and self.f_bump.active:
            revision += 1
        values["revision"] = revision
        images = list(self.images)
        old_urls = list((original or {}).get("images", []))
        self.set_busy(True, "Saving...")

        def work():
            try:
                urls = []
                for n, image in enumerate(images):
                    if "url" in image:
                        urls.append(image["url"])
                        continue
                    ui(self.set_busy, True, f"Uploading image {n + 1} of {len(images)}...")
                    data, ext, content_type = core.prepare_image_for_upload(image["path"])
                    digest = hashlib.sha256(data).hexdigest()[:24]
                    urls.append(app.api.upload_image(f"{values['id']}/{digest}.{ext}", data, content_type))
                values["images"] = urls
                ui(self.set_busy, True, "Saving...")
                saved = app.api.save_plugin(values)
                orphans = [u for u in old_urls if u not in urls]
                if orphans:
                    try:
                        app.api.delete_images(orphans)
                    except ApiError:
                        pass
                ui(self.saved, core.parse_catalog([saved]), None)
            except ApiError as e:
                ui(self.saved, None, str(e))
            except Exception as e:
                core.log_exception(type(e), e, e.__traceback__)
                ui(self.saved, None, f"Unexpected error: {e}")
        bg(work)

    def saved(self, saved, error):
        app = self.app
        self.set_busy(False)
        if error:
            self.status.text, self.status.color = error, C(DANGER)
            app.toast(f"Save failed: {error}", "error", 6)
            if not app.api.signed_in:
                app.sign_out(expired=True)
            return
        plugin = saved[0] if saved else None
        if plugin:
            app.admin_rows = [p for p in app.admin_rows if p["id"] != plugin["id"]] + [plugin]
            app.admin_rows.sort(key=lambda p: p["name"].lower())
            self.open(plugin)
        app.toast("Saved - the change is live for every user.", "success")
        app.load_admin_rows()
        app.load_catalog()

    def delete(self):
        plugin = self.original
        if self.busy or not plugin:
            return
        confirm("Delete plugin", f"Delete \"{plugin['name']}\" from the catalog? "
                                 "It disappears for every user.", lambda: self.do_delete(plugin))

    def do_delete(self, plugin):
        app = self.app
        self.set_busy(True, "Deleting...")

        def work():
            try:
                app.api.delete_plugin(plugin["id"])
                try:
                    app.api.delete_images(plugin["images"])
                except ApiError:
                    pass
                ui(self.deleted, plugin, None)
            except ApiError as e:
                ui(self.deleted, plugin, str(e))
        bg(work)

    def deleted(self, plugin, error):
        app = self.app
        self.set_busy(False)
        if error:
            self.status.text, self.status.color = error, C(DANGER)
            return
        app.admin_rows = [p for p in app.admin_rows if p["id"] != plugin["id"]]
        self.set_dirty(False)
        app.toast("Plugin deleted.", "success")
        app.load_catalog()
        app.go("admin")

    def request_leave(self):
        if self.busy:
            return
        if self.dirty:
            confirm("Unsaved changes", "Leave the editor and discard your changes?",
                    lambda: (self.set_dirty(False), self.app.go("admin")), yes="Discard")
        else:
            self.app.go("admin")


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------

class RobloxToolsApp(App):
    title = "RobloxTools"

    def build(self):
        core.init_data_dir(self.user_data_dir)
        self.settings = core.load_json(core.settings_path(), {})
        if not isinstance(self.settings, dict):
            self.settings = {}
        UI.scale = min(1.5, max(0.7, float(self.settings.get("ui_scale", 1.0))))
        UI.text = min(1.5, max(0.7, float(self.settings.get("text_scale", 1.0))))

        self.api = core.SupabaseClient()
        self.plugins, self.catalog_time = core.load_cached_catalog()
        self.catalog_error, self.loading = "", False
        self.registry = {}
        self.downloading = {}
        self.admin_rows, self.admin_loaded, self.admin_loading = [], False, False
        self.login_failures = []
        self._toast = None

        Window.clearcolor = C(BG_APP)
        Window.softinput_mode = "below_target"
        Window.bind(on_keyboard=self.on_key)

        self.root_widget = BoxLayout(orientation="vertical")
        self.build_ui("browse")
        Clock.schedule_once(lambda dt: self.start_up(), 0.3)
        return self.root_widget

    # -- ui construction
    def build_ui(self, current="browse"):
        self.root_widget.clear_widgets()
        self.sm = ScreenManager(transition=FadeTransition(duration=0.12))
        self.pages = {}
        for cls in (BrowsePage, PluginPage, LibraryPage, ExportPage, SettingsPage, AdminPage, EditorPage):
            page = cls(self)
            self.pages[page.name] = page
            self.sm.add_widget(page)
        self.root_widget.add_widget(self.sm)

        self.nav = BoxLayout(orientation="horizontal", size_hint_y=None, height=S(60),
                             padding=[S(8), S(6)], spacing=S(6))
        self.nav_buttons = {}
        for key, label in (("browse", "Browse"), ("library", "Library"), ("export", "Export"), ("settings", "Settings")):
            btn = Btn(label, kind="ghost", size=13, h=48, on_release=lambda _b, k=key: self.go(k))
            self.nav_buttons[key] = btn
            self.nav.add_widget(btn)
        self.root_widget.add_widget(self.nav)
        self.go(current)

    def apply_scale(self, current):
        UI.scale = float(self.settings.get("ui_scale", 1.0))
        UI.text = float(self.settings.get("text_scale", 1.0))
        Clock.schedule_once(lambda dt: self.build_ui(current), 0)

    def go(self, name):
        same = self.sm.current == name
        self.sm.current = name
        if same and name in MAIN_PAGES:
            self.pages[name].refresh()
        on_main = name in MAIN_PAGES
        set_visible(self.nav, on_main, S(60))
        for key, btn in self.nav_buttons.items():
            btn.set_kind("soft" if key == name else "ghost")

    def back(self):
        cur = self.sm.current
        if cur == "plugin":
            self.go(self.pages["plugin"].back_to)
        elif cur == "editor":
            self.pages["editor"].request_leave()
        elif cur == "admin":
            self.go("settings")
        elif cur != "browse":
            self.go("browse")
        else:
            self.stop()

    def on_key(self, _window, key, *_args):
        if key == 27:
            for child in list(Window.children):
                if isinstance(child, ModalView):
                    child.dismiss()
                    return True
            self.back()
            return True
        return False

    # -- lifecycle
    def on_pause(self):
        return True

    def on_resume(self):
        if self.sm.current == "settings":
            self.pages["settings"].render_storage()

    def start_up(self):
        if not has_storage_access():
            request_storage_access()
        self.registry = core.load_installed()
        self.load_catalog()

    def save_settings(self):
        core.save_json(core.settings_path(), self.settings)

    # -- toast
    def toast(self, text, kind="info", duration=3.2):
        colors = {"success": SUCCESS, "error": DANGER, "warning": WARNING, "info": ACCENT}
        if self._toast is not None and self._toast.parent:
            Window.remove_widget(self._toast)
        card = Card(bg=BG_HOVER, radius=14, pad=[16, 12], size_hint=(None, None), width=Window.width - S(32))
        card.size_hint_y = None
        card.add_widget(Lbl(text, size=14, color=colors.get(kind, TEXT)))
        card.pos = (S(16), S(76))
        self._toast = card
        Window.add_widget(card)
        anim = Animation(opacity=1, duration=0.01) + Animation(opacity=1, duration=duration) + Animation(opacity=0, duration=0.4)
        anim.bind(on_complete=lambda *_: card.parent and Window.remove_widget(card))
        anim.start(card)

    # -- catalog
    def load_catalog(self):
        if self.loading:
            return
        self.loading, self.catalog_error = True, ""
        self.pages["browse"].update_status()

        def work():
            try:
                rows = self.api.fetch_plugins()
                core.save_cached_catalog(rows)
                ui(self.catalog_done, core.parse_catalog(rows), "")
            except ApiError as e:
                ui(self.catalog_done, None, str(e))
        bg(work)

    def catalog_done(self, plugins, error):
        self.loading = False
        if plugins is not None:
            self.plugins, self.catalog_time = plugins, time.time()
        else:
            self.catalog_error = error
        self.pages["browse"].rebuild()
        self.refresh_buttons()
        if self.sm.current in ("library", "settings"):
            self.pages[self.sm.current].refresh()

    # -- plugin state / downloads
    def plugin_state(self, plugin):
        pid = plugin["id"]
        if pid in self.downloading:
            return "downloading"
        entry = self.registry.get(pid)
        if entry and entry.get("filename") and os.path.exists(os.path.join(core.PLUGINS_DIR, entry["filename"])):
            return "update" if int(entry.get("revision", 1)) < plugin["revision"] else "installed"
        return "none"

    def style_action(self, btn, plugin):
        state = self.plugin_state(plugin)
        if state == "downloading":
            btn.text, kind = f"Downloading {self.downloading[plugin['id']]}%", "secondary"
        elif state == "installed":
            btn.text, kind = "Installed", "success"
        elif state == "update":
            btn.text, kind = "Update available", "warning"
        else:
            btn.text, kind = "Download", "accent"
        btn.set_kind(kind)

    def on_action(self, plugin):
        state = self.plugin_state(plugin)
        if state in ("none", "update"):
            self.start_download(plugin)
        elif state == "installed":
            self.open_plugin(plugin, self.sm.current if self.sm.current in MAIN_PAGES else "browse")

    def refresh_buttons(self):
        self.pages["browse"].refresh_buttons()
        self.pages["plugin"].refresh_buttons()

    def start_download(self, plugin):
        pid = plugin["id"]
        if pid in self.downloading:
            return
        if not has_storage_access():
            self.toast("Allow storage access in Settings first.", "warning")
            return
        self.downloading[pid] = 0
        self.refresh_buttons()
        dest = os.path.join(core.PLUGINS_DIR, core.plugin_filename(plugin))

        def work():
            last = [-1]

            def progress(done, total):
                pct = int(done * 100 / total) if total else 0
                if pct != last[0]:
                    last[0] = pct
                    ui(self.download_progress, pid, pct)
            try:
                core.download_file(plugin["download_url"], dest, progress)
                ui(self.download_done, plugin, dest, None)
            except DownloadError as e:
                ui(self.download_done, plugin, dest, str(e))
            except Exception as e:
                core.log_exception(type(e), e, e.__traceback__)
                ui(self.download_done, plugin, dest, f"Unexpected error: {e}")
        bg(work)

    def download_progress(self, pid, pct):
        if pid in self.downloading:
            self.downloading[pid] = pct
            self.refresh_buttons()

    def download_done(self, plugin, dest, error):
        self.downloading.pop(plugin["id"], None)
        if error:
            self.toast(error, "error", 6)
        else:
            self.registry = core.load_installed()
            self.registry[plugin["id"]] = {
                "filename": os.path.basename(dest), "revision": plugin["revision"], "name": plugin["name"],
                "short_description": plugin["short_description"], "thumb": plugin["thumb"],
                "installed_at": time.time(),
            }
            core.save_installed(self.registry)
            self.toast(f"{plugin['name']} saved to the plugins folder.", "success")
        self.refresh_buttons()
        if self.sm.current == "library":
            self.pages["library"].refresh()

    def uninstall_dialog(self, plugin):
        confirm("Remove plugin", f"Delete \"{plugin['name']}\" from your plugins folder?",
                lambda: self.uninstall(plugin))

    def uninstall(self, plugin):
        entry = self.registry.get(plugin["id"])
        if entry:
            try:
                os.remove(os.path.join(core.PLUGINS_DIR, entry["filename"]))
            except OSError:
                pass
            self.registry.pop(plugin["id"], None)
            core.save_installed(self.registry)
        self.toast("Plugin removed.", "success")
        self.refresh_buttons()

    def open_plugin(self, plugin, back_to):
        self.pages["plugin"].show(plugin, back_to)
        self.go("plugin")

    # -- admin
    def load_admin_rows(self):
        if not self.api.signed_in or self.admin_loading:
            return
        self.admin_loading = True

        def work():
            try:
                rows = core.parse_catalog(self.api.fetch_plugins(include_hidden=True))
                ui(self.admin_rows_loaded, rows, None)
            except ApiError as e:
                ui(self.admin_rows_loaded, None, str(e))
        bg(work)

    def admin_rows_loaded(self, rows, error):
        self.admin_loading = False
        if error:
            self.toast(error, "error", 5)
            if not self.api.signed_in:
                self.sign_out(expired=True)
            return
        self.admin_rows, self.admin_loaded = rows, True
        if self.sm.current == "admin":
            self.pages["admin"].render()

    def edit_plugin(self, plugin):
        self.pages["editor"].open(plugin)
        self.go("editor")

    def sign_out(self, expired=False):
        if self.pages["editor"].dirty and not expired:
            confirm("Unsaved changes", "The editor has unsaved changes. Sign out anyway?",
                    lambda: self.sign_out(expired=True), yes="Sign out")
            return
        api = self.api

        def work():
            api.sign_out()
            ui(self.signed_out, expired)
        bg(work)

    def signed_out(self, expired):
        self.admin_rows, self.admin_loaded = [], False
        self.pages["editor"].set_dirty(False)
        self.go("settings")
        self.pages["settings"].render_admin()
        self.toast("Your admin session expired - sign in again." if expired else "Signed out.", "info")


if __name__ == "__main__":
    RobloxToolsApp().run()
