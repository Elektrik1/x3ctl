"""GTK 4 / libadwaita configuration window for the Attack Shark X3."""

from __future__ import annotations

import copy
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import protocol as p  # noqa: E402
from . import store  # noqa: E402
from .device import Device, DeviceError, find_config_interface  # noqa: E402
from .keys import name_for_usage, name_for_x11_keycode, usage_for_name  # noqa: E402

APP_ID = "io.github.x3ctl"
from . import __version__ as VERSION  # noqa: E402

# Flat action list for the drop-downs: (key, label). Two special entries at the end.
ACTION_CHOICES: list[tuple[str, str]] = [
    (key, f"{group} · {label}") for group, items in p.ACTION_GROUPS for key, label in items
]
CUSTOM_SHORTCUT = "__shortcut__"
MACRO = "__macro__"
ACTION_CHOICES += [(CUSTOM_SHORTCUT, "Custom keyboard shortcut…"), (MACRO, "Run macro…")]
ACTION_INDEX = {key: i for i, (key, _) in enumerate(ACTION_CHOICES)}

LOOP_LABELS = ["Play a fixed number of times", "Repeat until any key is pressed",
               "Repeat while the button is held"]


def rgba(colour: p.RGB) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = colour[0] / 255, colour[1] / 255, colour[2] / 255, 1.0
    return c


def to_rgb(c: Gdk.RGBA) -> p.RGB:
    return tuple(max(0, min(255, round(v * 255))) for v in (c.red, c.green, c.blue))  # type: ignore[return-value]


def parse_macro_text(text: str) -> list[p.MacroEvent]:
    """Lines of `<key> <down|up> <ms>`; `#` starts a comment."""
    events: list[p.MacroEvent] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace(",", " ").split()
        if len(parts) != 3 or parts[1].lower() not in ("down", "up"):
            raise ValueError(f"line {n}: expected `<key> down|up <ms>`, got {raw.strip()!r}")
        try:
            key = usage_for_name(parts[0])
            delay = int(parts[2])
        except ValueError as e:
            raise ValueError(f"line {n}: {e}") from None
        if delay < 0:
            raise ValueError(f"line {n}: delay must be positive")
        for piece in p.split_delay(delay):
            events.append(p.MacroEvent(key, parts[1].lower() == "down", piece))
    return events


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Attack Shark X3")
        self.set_default_size(1000, 800)

        last = store.load_last_applied()
        self.known_state = last is not None
        self.applied = last or p.Settings()
        self.settings = copy.deepcopy(self.applied)
        self.macros: dict = store.load_macros()
        self.device: Device | None = None
        self.device_path: str | None = None
        self.busy = False
        self._updating = False
        self._recording = False
        self._rec_last = 0.0
        self._rec_lines: list[list] = []
        self._rec_down: set[int] = set()

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.toasts.set_child(self.stack)

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        self.apply_all_btn = Gtk.Button(label="Apply all")
        self.apply_all_btn.add_css_class("suggested-action")
        self.apply_all_btn.set_tooltip_text("Send every setting to the mouse")
        self.apply_all_btn.connect("clicked", lambda *_: self.apply_all())
        header.pack_end(self.apply_all_btn)

        menu = Gio.Menu()
        menu.append("Reset editor to stock settings", "win.reset-stock")
        menu.append("About", "win.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_btn)

        self.live_label = Gtk.Label(margin_start=6)
        self.live_label.add_css_class("dim-label")
        self.live_label.add_css_class("numeric")
        header.pack_start(self.live_label)
        self.battery: int | None = None
        self.current_stage: int | None = None
        self._add_action("reset-stock", self.reset_stock)
        self._add_action("about", self.show_about)


        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._banner_clicked)
        self.dirty_banner = Adw.Banner(title="You have changes that haven't been sent to the mouse",
                                       button_label="Apply all")
        self.dirty_banner.connect("button-clicked", lambda *_: self.apply_all())

        bar = Adw.ViewSwitcherBar(stack=self.stack)
        switcher.bind_property("visible", bar, "reveal", 0)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.add_top_bar(self.dirty_banner)
        view.set_content(self.toasts)
        view.add_bottom_bar(bar)
        self.set_content(view)

        self._build_dpi_page()
        self._build_buttons_page()
        self._build_macro_page()
        self._build_performance_page()
        self._build_profiles_page()
        self._build_device_page()

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        keys.connect("key-released", self._on_key_released)
        self.add_controller(keys)

        self.refresh_widgets()
        self.log("x3ctl " + VERSION)
        if not self.known_state:
            self.log("No settings have been sent from this app yet. The mouse can't report its "
                     "current settings, so the editor shows its stock values.")
        self.poll_device()
        GLib.timeout_add_seconds(2, self.poll_device)
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------ helpers

    def _add_action(self, name, cb):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda *_: cb())
        self.add_action(action)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), f"{stamp}  {line}\n")
        self.log_view.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
        GLib.idle_add(lambda: self.log_view.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 0) and False)

    def _page(self, name, title, icon) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, name, title, icon)
        return page

    def _apply_button(self, label, cb) -> Gtk.Button:
        btn = Gtk.Button(label=label, halign=Gtk.Align.END, margin_top=12)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", lambda *_: cb())
        return btn

    def changed(self) -> None:
        if self._updating:
            return
        self.dirty_banner.set_revealed(self.settings.to_dict() != self.applied.to_dict())

    # ---------------------------------------------------------------- DPI page

    def _build_dpi_page(self):
        page = self._page("dpi", "DPI", "input-mouse-symbolic")
        group = Adw.PreferencesGroup(
            title="DPI stages",
            description="The DPI button cycles through the switched-on stages. The radio button "
                        "picks the stage the mouse uses straight after you apply. The colour is "
                        "stored, but the X3 has no stage light to show it.")
        presets = Gio.Menu()
        presets.append("This mouse's stock stages (800–26000, 5)", "win.preset-stock")
        presets.append("Vendor factory table (6 stages)", "win.preset-vendor")
        self._add_action("preset-stock", lambda: self._load_stages(p.STOCK_STAGES, p.STOCK_ACTIVE))
        self._add_action("preset-vendor",
                         lambda: self._load_stages(p.VENDOR_FACTORY_STAGES, p.VENDOR_FACTORY_ACTIVE))
        group.set_header_suffix(Gtk.MenuButton(label="Presets", menu_model=presets,
                                               valign=Gtk.Align.CENTER))
        page.add(group)

        self.stage_rows = []
        radio_group = None
        for i in range(p.DPI_SLOTS):
            row = Adw.ActionRow(title=f"Stage {i + 1}")
            radio = Gtk.CheckButton(valign=Gtk.Align.CENTER, tooltip_text="Start on this stage")
            if radio_group:
                radio.set_group(radio_group)
            radio_group = radio_group or radio
            row.add_prefix(radio)
            spin = Gtk.SpinButton.new_with_range(p.DPI_MIN, p.DPI_MAX, p.DPI_STEP)
            spin.set_valign(Gtk.Align.CENTER)
            spin.set_width_chars(6)
            colour = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                           valign=Gtk.Align.CENTER)
            switch = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Include in the DPI cycle")
            row.add_suffix(spin)
            row.add_suffix(colour)
            row.add_suffix(switch)
            group.add(row)
            radio.connect("toggled", self._on_stage_active, i)
            spin.connect("value-changed", self._on_stage_dpi, i)
            colour.connect("notify::rgba", self._on_stage_colour, i)
            switch.connect("notify::active", self._on_stage_enabled, i)
            self.stage_rows.append((row, radio, spin, colour, switch))

        sensor = Adw.PreferencesGroup(
            title="Sensor",
            description="These options travel in the same report as the DPI stages, so they are "
                        "applied together.")
        page.add(sensor)
        self.liftoff_row = Adw.ComboRow(title="Lift-off distance",
                                        subtitle="How high the mouse can lift before tracking stops",
                                        model=Gtk.StringList.new(["1 mm", "2 mm"]))
        self.ripple_row = Adw.SwitchRow(title="Ripple control",
                                        subtitle="Smooths sensor jitter at very high DPI (adds some latency)")
        self.angle_row = Adw.SwitchRow(title="Angle snapping",
                                       subtitle="Straightens near-horizontal and near-vertical movement")
        self.motion_row = Adw.SwitchRow(title="Motion sync",
                                        subtitle="Aligns sensor reads with USB polling for more even motion")
        for row in (self.liftoff_row, self.ripple_row, self.angle_row, self.motion_row):
            sensor.add(row)
        self.liftoff_row.connect("notify::selected", self._on_toggle)
        for row in (self.ripple_row, self.angle_row, self.motion_row):
            row.connect("notify::active", self._on_toggle)

        foot = Adw.PreferencesGroup()
        foot.add(self._apply_button("Apply DPI and sensor", self.apply_dpi))
        page.add(foot)

    def _load_stages(self, stages, active):
        self.settings.stages = [p.Stage(s.dpi, s.enabled, s.colour) for s in stages]
        self.settings.active_stage = active
        self.refresh_widgets()
        self.changed()

    def _on_stage_active(self, radio, i):
        if radio.get_active() and not self._updating:
            self.settings.active_stage = i
            self.changed()

    def _on_stage_dpi(self, spin, i):
        if not self._updating:
            self.settings.stages[i].dpi = int(spin.get_value())
            self.changed()

    def _on_stage_colour(self, btn, _pspec, i):
        if not self._updating:
            self.settings.stages[i].colour = to_rgb(btn.get_rgba())
            self.changed()

    def _on_stage_enabled(self, switch, _pspec, i):
        if self._updating:
            return
        stage = self.settings.stages[i]
        stage.enabled = switch.get_active()
        if stage.enabled and stage.dpi < p.DPI_MIN:
            stage.dpi = 1600
            self.refresh_widgets()
        self._sync_stage_sensitivity()
        self.changed()

    def _sync_stage_sensitivity(self):
        for i, (row, radio, spin, colour, switch) in enumerate(self.stage_rows):
            on = self.settings.stages[i].enabled
            radio.set_sensitive(on)
            spin.set_sensitive(on)
            colour.set_sensitive(on)
            row.set_subtitle("" if on else "Off")

    def _on_toggle(self, *_):
        if self._updating:
            return
        t = self.settings.toggles
        t.lift_off_2mm = self.liftoff_row.get_selected() == 1
        t.ripple_control = self.ripple_row.get_active()
        t.angle_snap = self.angle_row.get_active()
        t.motion_sync = self.motion_row.get_active()
        self.changed()

    # ------------------------------------------------------------ buttons page

    def _build_buttons_page(self):
        page = self._page("buttons", "Buttons", "input-mouse-symbolic")
        page.set_icon_name("view-grid-symbolic")
        group = Adw.PreferencesGroup(
            title="Button mapping",
            description="All buttons are written together. At least one must stay a left click, "
                        "so you can always click to undo a change.")
        page.add(group)
        self.button_rows = {}
        for entry, name in p.PHYSICAL_BUTTONS:
            group.add(self._button_row(entry, name))

        hidden = Adw.PreferencesGroup(
            title="Other entries",
            description="The report has 18 entries. These ones trigger nothing on the X3, but "
                        "they are listed so nothing is written blind.")
        expander = Adw.ExpanderRow(title="Show the other 11 entries")
        physical = {e for e, _ in p.PHYSICAL_BUTTONS}
        for entry in range(p.BUTTON_ENTRIES):
            if entry not in physical:
                expander.add_row(self._button_row(entry, f"Entry {entry + 1}"))
        hidden.add(expander)
        page.add(hidden)

        foot = Adw.PreferencesGroup()
        box = Gtk.Box(spacing=12, halign=Gtk.Align.END, margin_top=12)
        restore = Gtk.Button(label="Restore factory mapping")
        restore.add_css_class("pill")
        restore.connect("clicked", lambda *_: self.restore_factory_buttons())
        box.append(restore)
        apply = self._apply_button("Apply buttons", self.apply_buttons)
        apply.set_margin_top(0)
        box.append(apply)
        foot.add(box)
        page.add(foot)

    def _button_row(self, entry, name):
        row = Adw.ActionRow(title=name)
        if entry == p.MODE_BUTTON_ENTRY:
            row.set_subtitle("Factory action switches Bluetooth channels")
        model = Gtk.StringList.new([label for _, label in ACTION_CHOICES])
        dropdown = Gtk.DropDown(model=model, valign=Gtk.Align.CENTER, enable_search=True)
        dropdown.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        shortcut = Gtk.Entry(placeholder_text="ctrl+shift+t", valign=Gtk.Align.CENTER, width_chars=14,
                             tooltip_text="Modifiers ctrl, shift, alt, super joined with + and a key")
        macro = Gtk.SpinButton.new_with_range(1, p.BUTTON_ENTRIES, 1)
        macro.set_valign(Gtk.Align.CENTER)
        macro.set_tooltip_text("Macro slot (upload it on the Macros page)")
        row.add_suffix(shortcut)
        row.add_suffix(macro)
        row.add_suffix(dropdown)
        dropdown.connect("notify::selected", self._on_button_changed, entry)
        shortcut.connect("changed", self._on_button_changed, None, entry)
        macro.connect("value-changed", self._on_button_changed, None, entry)
        self.button_rows[entry] = (row, dropdown, shortcut, macro)
        return row

    def _on_button_changed(self, _w, _pspec, entry):
        if self._updating:
            return
        row, dropdown, shortcut, macro = self.button_rows[entry]
        key = ACTION_CHOICES[dropdown.get_selected()][0]
        shortcut.set_visible(key == CUSTOM_SHORTCUT)
        macro.set_visible(key == MACRO)
        if key == CUSTOM_SHORTCUT:
            text = shortcut.get_text().strip().lower().replace(" ", "")
            spec = "key:" + text
            try:
                p.parse_action(spec)
                shortcut.remove_css_class("error")
            except ValueError:
                shortcut.add_css_class("error")
                return
        elif key == MACRO:
            spec = f"macro:{int(macro.get_value())}"
        else:
            spec = key
        self.settings.buttons[entry] = spec
        self.changed()

    # -------------------------------------------------------------- macro page

    def _build_macro_page(self):
        page = self._page("macros", "Macros", "media-record-symbolic")
        group = Adw.PreferencesGroup(
            title="Macro",
            description="Upload a key sequence to a slot, then set a button to “Run macro…” with "
                        "the same slot number on the Buttons page. Up to 50 events.")
        page.add(group)
        self.macro_slot = Adw.SpinRow.new_with_range(1, p.BUTTON_ENTRIES, 1)
        self.macro_slot.set_title("Slot")
        self.macro_slot.connect("notify::value", lambda *_: self._load_macro_slot())
        self.macro_loop = Adw.ComboRow(title="Playback", model=Gtk.StringList.new(LOOP_LABELS))
        self.macro_repeat = Adw.SpinRow.new_with_range(1, 255, 1)
        self.macro_repeat.set_title("Repeat count")
        self.macro_loop.connect("notify::selected", lambda *_: self.macro_repeat.set_sensitive(
            self.macro_loop.get_selected() == p.LOOP_TIMES))
        for row in (self.macro_slot, self.macro_loop, self.macro_repeat):
            group.add(row)

        events = Adw.PreferencesGroup(
            title="Events",
            description="One event per line: key, down or up, and a delay in milliseconds. Keys are "
                        "letters, digits, names like enter, space, f5, lctrl, or mouse_left, "
                        "mouse_right, mouse_middle, mouse_forward, mouse_backward.")
        self.record_btn = Gtk.ToggleButton(label="Record keys", valign=Gtk.Align.CENTER)
        self.record_btn.connect("toggled", self._on_record_toggled)
        events.set_header_suffix(self.record_btn)
        page.add(events)
        self.macro_text = Gtk.TextView(monospace=True, top_margin=8, bottom_margin=8,
                                       left_margin=10, right_margin=10)
        self.macro_text.set_size_request(-1, 220)
        frame = Gtk.Frame(child=Gtk.ScrolledWindow(child=self.macro_text, min_content_height=220))
        events.add(frame)
        self.macro_count = Gtk.Label(xalign=0, margin_top=6)
        self.macro_count.add_css_class("dim-label")
        events.add(self.macro_count)
        self.macro_text.get_buffer().connect("changed", lambda *_: self._update_macro_count())

        foot = Adw.PreferencesGroup()
        foot.add(self._apply_button("Upload macro", self.upload_macro))
        page.add(foot)
        self._load_macro_slot()

    def _load_macro_slot(self):
        slot = str(int(self.macro_slot.get_value()))
        m = self.macros.get(slot, {})
        self.macro_text.get_buffer().set_text(
            m.get("text", "# example: type “hi”\nh down 20\nh up 20\ni down 20\ni up 20\n"))
        self.macro_loop.set_selected(int(m.get("loop", p.LOOP_TIMES)))
        self.macro_repeat.set_value(int(m.get("repeat", 1)))

    def _macro_source(self) -> str:
        buf = self.macro_text.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _update_macro_count(self):
        try:
            n = len(parse_macro_text(self._macro_source()))
            self.macro_count.set_text(f"{n} of {p.MACRO_MAX_EVENTS} events")
            self.macro_count.remove_css_class("error")
        except ValueError as e:
            self.macro_count.set_text(str(e))
            self.macro_count.add_css_class("error")

    def _on_record_toggled(self, btn):
        self._recording = btn.get_active()
        if self._recording:
            self._rec_lines = []
            self._rec_down = set()
            self._rec_last = 0.0
            btn.set_label("Stop recording")
            btn.add_css_class("destructive-action")
            self.macro_text.set_sensitive(False)
            self.toast("Recording: type the sequence, then press Stop recording")
        else:
            btn.set_label("Record keys")
            btn.remove_css_class("destructive-action")
            self.macro_text.set_sensitive(True)
            if self._rec_lines:
                self._rec_lines[-1][2] = 20
                text = "".join(f"{k} {d} {ms}\n" for k, d, ms in self._rec_lines)
                self.macro_text.get_buffer().set_text(text)

    def _record(self, keycode, down):
        name = name_for_x11_keycode(keycode)
        if name is None:
            self.toast("That key can't be used in a macro")
            return
        if down and keycode in self._rec_down:
            return  # auto-repeat
        (self._rec_down.add if down else self._rec_down.discard)(keycode)
        now = time.monotonic()
        # Each event's delay is the pause before the next one; fill in the previous.
        if self._rec_lines:
            self._rec_lines[-1][2] = max(10, min(int((now - self._rec_last) * 1000), 1270))
        self._rec_last = now
        self._rec_lines.append([name, "down" if down else "up", 20])

    def _on_key_pressed(self, _c, _keyval, keycode, _state):
        if not self._recording:
            return False
        self._record(keycode, True)
        return True

    def _on_key_released(self, _c, _keyval, keycode, _state):
        if self._recording:
            self._record(keycode, False)

    # -------------------------------------------------------- performance page

    def _build_performance_page(self):
        page = self._page("performance", "Tuning", "power-profile-performance-symbolic")
        poll = Adw.PreferencesGroup(title="Polling rate")
        self.poll_row = Adw.ComboRow(
            title="Report rate", subtitle="Higher is smoother but uses more battery",
            model=Gtk.StringList.new([f"{hz} Hz" for hz in p.POLLING_RATES]))
        self.poll_row.connect("notify::selected", self._on_poll)
        poll.add(self.poll_row)
        poll_foot = Adw.PreferencesGroup()
        poll_foot.add(self._apply_button("Apply polling rate", self.apply_polling))
        page.add(poll)
        page.add(poll_foot)

        power = Adw.PreferencesGroup(
            title="Power and debounce",
            description="Sleep timers are accepted by the mouse, but nobody has measured "
                        "their effect yet.")
        self.sleep_row = Adw.SpinRow.new_with_range(1, 60, 1)
        self.sleep_row.set_title("Sleep after (minutes)")
        self.deep_row = Adw.SpinRow.new_with_range(1, 60, 1)
        self.deep_row.set_title("Deep sleep after (minutes)")
        self.debounce_row = Adw.SpinRow.new_with_range(2, 25, 1)
        self.debounce_row.set_title("Click debounce")
        for row in (self.sleep_row, self.deep_row, self.debounce_row):
            row.connect("notify::value", self._on_power)
            power.add(row)
        page.add(power)
        power_foot = Adw.PreferencesGroup()
        power_foot.add(self._apply_button("Apply power and debounce", self.apply_power))
        page.add(power_foot)

        info = Adw.PreferencesGroup(
            title="Scroll direction",
            description="Linux already lets you set natural scrolling for mice separately from "
                        "the touchpad: Settings → Mouse &amp; Touchpad.")
        page.add(info)

    def _on_poll(self, *_):
        if not self._updating:
            self.settings.polling_hz = p.POLLING_RATES[self.poll_row.get_selected()]
            self.changed()

    def _on_power(self, *_):
        wire = int(self.debounce_row.get_value())
        self.debounce_row.set_subtitle(f"Value {wire} = {p.effective_debounce_ms(wire)} ms real "
                                       f"debounce (the field counts 2 ms steps)")
        if self._updating:
            return
        self.settings.sleep_minutes = int(self.sleep_row.get_value())
        self.settings.deep_sleep_minutes = int(self.deep_row.get_value())
        self.settings.debounce = wire
        self.changed()

    # ----------------------------------------------------------- profiles page

    def _build_profiles_page(self):
        page = self._page("profiles", "Profiles", "document-save-symbolic")
        group = Adw.PreferencesGroup(
            title="Saved profiles",
            description="Profiles are stored on this computer. Loading one fills the editor; "
                        "apply it to send it to the mouse.")
        self.profile_entry = Adw.EntryRow(title="Save current settings as…", show_apply_button=True)
        self.profile_entry.connect("apply", self._on_save_profile)
        group.add(self.profile_entry)
        page.add(group)
        self.profile_group = Adw.PreferencesGroup()
        page.add(self.profile_group)
        self._profile_rows = []
        self._refresh_profiles()

    def _refresh_profiles(self):
        for row in self._profile_rows:
            self.profile_group.remove(row)
        self._profile_rows = []
        names = store.profile_names()
        if not names:
            row = Adw.ActionRow(title="No profiles yet")
            row.add_css_class("dim-label")
            self.profile_group.add(row)
            self._profile_rows.append(row)
        for name in names:
            row = Adw.ActionRow(title=GLib.markup_escape_text(name))
            load = Gtk.Button(label="Load", valign=Gtk.Align.CENTER)
            load.connect("clicked", lambda _b, n=name: self._load_profile(n, apply=False))
            send = Gtk.Button(label="Load and apply", valign=Gtk.Align.CENTER)
            send.add_css_class("suggested-action")
            send.connect("clicked", lambda _b, n=name: self._load_profile(n, apply=True))
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Delete profile")
            delete.add_css_class("flat")
            delete.connect("clicked", lambda _b, n=name: self._delete_profile(n))
            for w in (load, send, delete):
                row.add_suffix(w)
            self.profile_group.add(row)
            self._profile_rows.append(row)

    def _on_save_profile(self, entry):
        name = entry.get_text().strip()
        if not name:
            return
        store.save_profile(name, self.settings)
        entry.set_text("")
        self._refresh_profiles()
        self.toast(f"Saved profile “{name}”")

    def _load_profile(self, name, apply):
        s = store.load_profile(name)
        if s is None:
            self.toast(f"Couldn't read profile “{name}”")
            return
        self.settings = s
        self.refresh_widgets()
        self.changed()
        self.toast(f"Loaded “{name}”")
        if apply:
            self.apply_all()

    def _delete_profile(self, name):
        store.delete_profile(name)
        self._refresh_profiles()
        self.toast(f"Deleted “{name}”")

    # ------------------------------------------------------------- device page

    def _build_device_page(self):
        page = self._page("device", "Device", "preferences-system-symbolic")
        group = Adw.PreferencesGroup(title="Connection")
        self.dev_row = Adw.ActionRow(title="Receiver", subtitle="Not found")
        self.stage_row = Adw.ActionRow(title="Current DPI stage",
                                       subtitle="Press the DPI button to update")
        group.add(self.dev_row)
        group.add(self.stage_row)
        self.battery_row = Adw.ActionRow(title="Battery", subtitle="Waiting for the mouse to report")
        group.add(self.battery_row)
        page.add(group)

        log_group = Adw.PreferencesGroup(
            title="Log", description="Raw reports sent to the mouse and events it sends back.")
        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8,
                                     bottom_margin=8, left_margin=10, right_margin=10)
        log_group.add(Gtk.Frame(child=Gtk.ScrolledWindow(child=self.log_view, min_content_height=300)))
        page.add(log_group)

    # ------------------------------------------------------------ sync widgets

    def refresh_widgets(self):
        self._updating = True
        try:
            s = self.settings
            for i, (row, radio, spin, colour, switch) in enumerate(self.stage_rows):
                stage = s.stages[i]
                spin.set_value(max(stage.dpi, p.DPI_MIN))
                colour.set_rgba(rgba(stage.colour))
                switch.set_active(stage.enabled)
                radio.set_active(i == s.active_stage)
            self._sync_stage_sensitivity()
            self.liftoff_row.set_selected(1 if s.toggles.lift_off_2mm else 0)
            self.ripple_row.set_active(s.toggles.ripple_control)
            self.angle_row.set_active(s.toggles.angle_snap)
            self.motion_row.set_active(s.toggles.motion_sync)
            self.poll_row.set_selected(p.POLLING_RATES.index(s.polling_hz)
                                       if s.polling_hz in p.POLLING_RATES else 3)
            self.sleep_row.set_value(max(1, s.sleep_minutes))
            self.deep_row.set_value(max(1, s.deep_sleep_minutes))
            self.debounce_row.set_value(s.debounce)
            for entry, (row, dropdown, shortcut, macro) in self.button_rows.items():
                spec = s.buttons[entry]
                if spec.startswith("key:"):
                    dropdown.set_selected(ACTION_INDEX[CUSTOM_SHORTCUT])
                    shortcut.set_text(spec[4:])
                elif spec.startswith("macro:"):
                    dropdown.set_selected(ACTION_INDEX[MACRO])
                    macro.set_value(int(spec[6:]))
                elif spec in ACTION_INDEX:
                    dropdown.set_selected(ACTION_INDEX[spec])
                else:
                    # Anything not in the list (e.g. "none", raw) is shown as a shortcut entry.
                    dropdown.set_selected(ACTION_INDEX["button_off"])
                shortcut.set_visible(spec.startswith("key:"))
                macro.set_visible(spec.startswith("macro:"))
        finally:
            self._updating = False
        self._on_power()

    def reset_stock(self):
        self.settings = p.Settings()
        self.refresh_widgets()
        self.changed()
        self.toast("Editor reset to stock settings. Apply to send them.")

    # ------------------------------------------------------------------ device

    def poll_device(self):
        info = find_config_interface()
        path = info.path if info else None
        if path == self.device_path and (self.device or not info or not info.readable):
            if info and not info.readable:
                self._show_permission_banner()
            return True
        if self.device:
            self.device.close()
            self.device = None
        self.device_path = path
        if info is None:
            self._set_status("Receiver not connected", "Plug in the 2.4 GHz receiver or the USB cable")
            self.banner.set_title("The mouse receiver isn't connected")
            self.banner.set_button_label(None)
            self.banner.set_revealed(True)
            self.log("receiver not found")
            return True
        try:
            self.device = Device(info)
        except DeviceError as e:
            self._set_status("No permission", info.path)
            self._show_permission_banner()
            self.log(str(e))
            return True
        self.banner.set_revealed(False)
        self._set_status("Connected", f"{info.name} on {info.path}")
        self.log(f"opened {info.path} ({info.name}, PID {info.product_id:04X})")
        self.device.start_status_reader(
            lambda ev: GLib.idle_add(self._on_status_event, ev),
            lambda: GLib.idle_add(self._on_device_gone))
        return True

    def _show_permission_banner(self):
        self.banner.set_title("No permission to talk to the mouse. Install the udev rule once.")
        self.banner.set_button_label("How?")
        self.banner.set_revealed(True)

    def _banner_clicked(self, *_):
        dialog = Adw.AlertDialog(
            heading="Allow access to the receiver",
            body="Run this once in a terminal from the project folder, then unplug and replug "
                 "the receiver:\n\n./install.sh\n\nIt copies a udev rule to /etc/udev/rules.d so "
                 "your user can configure the mouse without sudo.")
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _set_status(self, short, detail):
        self.dev_row.set_title(f"Receiver: {short}")
        self.dev_row.set_subtitle(detail)
        connected = short == "Connected"
        self.apply_all_btn.set_sensitive(connected)

    def _on_status_event(self, ev: p.StatusEvent):
        pct = ev.battery_percent
        if pct is not None:
            # The receiver repeats this every couple of seconds; log only changes.
            if pct != self.battery:
                self.log("← " + ev.describe())
            self.battery = pct
        else:
            self.log("← " + ev.describe())
        if ev.code == 0x1000:
            self.current_stage = ev.value  # 1-based
        self._update_live()
        return False

    def _update_live(self):
        parts = []
        if self.current_stage is not None:
            i = self.current_stage - 1
            stages = self.applied.stages
            dpi = f" · {stages[i].dpi} DPI" if self.known_state and 0 <= i < len(stages) else ""
            text = f"Stage {self.current_stage}{dpi}"
            self.stage_row.set_subtitle(text)
            parts.append(f"{stages[i].dpi} DPI" if dpi else f"Stage {self.current_stage}")
        if self.battery is not None:
            self.battery_row.set_subtitle(f"{self.battery}% (reported in 10% steps)")
            parts.append(f"Battery {self.battery}%")
        self.live_label.set_text("  ·  ".join(parts))

    def _on_device_gone(self):
        if self.device:
            self.device.close()
        self.device = None
        self.device_path = None
        self.log("receiver disconnected")
        self.poll_device()
        return False

    # ----------------------------------------------------------------- applying

    def _send(self, what, reports, on_success, chunked=False):
        if self.busy:
            return
        if not self.device:
            self.toast("The mouse isn't connected")
            return
        self.busy = True
        self.apply_all_btn.set_sensitive(False)
        self.log(f"── {what}")
        device = self.device

        def work():
            error = None
            try:
                logger = lambda line: GLib.idle_add(self.log, line)  # noqa: E731
                if chunked:
                    device.send_chunked(reports, logger)
                else:
                    device.send(reports, logger)
            except DeviceError as e:
                error = str(e)
            GLib.idle_add(done, error)

        def done(error):
            self.busy = False
            self.apply_all_btn.set_sensitive(self.device is not None)
            if error:
                self.log("error: " + error)
                self.toast("Couldn't write to the mouse: " + error)
            else:
                self.known_state = True
                on_success()
                store.save_last_applied(self.applied)
                self.changed()
                self.toast(f"{what}: sent")
            return False

        threading.Thread(target=work, daemon=True).start()

    def _check_dpi(self) -> bool:
        s = self.settings
        if not any(st.enabled for st in s.stages):
            self.toast("Switch on at least one DPI stage")
            return False
        if not s.stages[s.active_stage].enabled:
            self.toast("The selected starting stage is switched off")
            return False
        return True

    def _check_buttons(self) -> bool:
        if not p.has_left_click(self.settings.buttons):
            self.toast("At least one button must stay a left click")
            return False
        return True

    def apply_dpi(self):
        if not self._check_dpi():
            return
        snap = copy.deepcopy(self.settings)

        def ok():
            self.applied.stages = snap.stages
            self.applied.active_stage = snap.active_stage
            self.applied.toggles = snap.toggles
            self.current_stage = snap.active_stage + 1
            self._update_live()
        self._send("DPI and sensor", [snap.dpi_report()], ok)

    def apply_polling(self):
        snap = copy.deepcopy(self.settings)
        self._send("Polling rate", [snap.polling_report()],
                   lambda: setattr(self.applied, "polling_hz", snap.polling_hz))

    def apply_power(self):
        snap = copy.deepcopy(self.settings)

        def ok():
            self.applied.sleep_minutes = snap.sleep_minutes
            self.applied.deep_sleep_minutes = snap.deep_sleep_minutes
            self.applied.debounce = snap.debounce
        self._send("Power and debounce", [snap.power_report()], ok)

    def apply_buttons(self):
        if not self._check_buttons():
            return
        snap = copy.deepcopy(self.settings)
        self._send("Buttons", [snap.button_report()],
                   lambda: setattr(self.applied, "buttons", snap.buttons))

    def restore_factory_buttons(self):
        self.settings.buttons = list(p.FACTORY_BUTTONS)
        self.refresh_widgets()
        self.apply_buttons()

    def apply_all(self):
        if not (self._check_dpi() and self._check_buttons()):
            return
        snap = copy.deepcopy(self.settings)
        reports = [snap.dpi_report(), snap.polling_report(), snap.power_report(), snap.button_report()]

        def ok():
            self.applied = copy.deepcopy(snap)
            self.current_stage = snap.active_stage + 1
            self._update_live()
        self._send("All settings", reports, ok)

    def upload_macro(self):
        try:
            events = parse_macro_text(self._macro_source())
        except ValueError as e:
            self.toast(str(e))
            return
        if not events:
            self.toast("The macro is empty")
            return
        if len(events) > p.MACRO_MAX_EVENTS:
            self.toast(f"Too many events: {len(events)} (maximum {p.MACRO_MAX_EVENTS})")
            return
        slot = int(self.macro_slot.get_value())
        loop = self.macro_loop.get_selected()
        repeat = int(self.macro_repeat.get_value())
        chunks = p.build_macro_reports(slot, events, repeat, loop)
        text = self._macro_source()

        def ok():
            self.macros[str(slot)] = {"text": text, "loop": loop, "repeat": repeat}
            store.save_macros(self.macros)
        self._send(f"Macro {slot}", chunks, ok, chunked=True)

    # -------------------------------------------------------------------- misc

    def show_about(self):
        about = Adw.AboutDialog(
            application_name="Attack Shark X3",
            application_icon="input-mouse",
            version=VERSION,
            comments="Configure the Attack Shark X3 mouse on Linux.\n\n"
                     "Built on the protocol work of Habibur Rahman's asctl for macOS. "
                     "Without it this app would not exist.",
            license_type=Gtk.License.MIT_X11,
            copyright="© 2026 Julius\nProtocol © 2026 Habibur Rahman",
            developers=["Julius"],
        )
        about.add_credit_section("Protocol reverse-engineering (asctl)",
                                 ["Habibur Rahman https://github.com/yourchocomate"])
        about.add_link("Original macOS project", "https://github.com/yourchocomate/attacksharkx3")
        about.present(self)

    def _on_close(self, *_):
        if self.device:
            self.device.close()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main() -> int:
    return App().run(sys.argv)
