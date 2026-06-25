"""Flet front-end — a faithful port of the original interface.

The layout, controls and device cards mirror the original app; only the event
handlers were rewritten to drive the new AudioRouter (the old ones were tied to
the previous monolithic engine). The UI never touches the router's internals.
"""

from __future__ import annotations

import atexit
import logging
import threading

import flet as ft

from ..audio import devices, latency
from ..audio.engine import AudioRouter
from ..audio.virtual_sink import VirtualSink
from ..config.settings import SettingsStore, TargetSettings

AUTOALIGN_SETTLE_S = 0.8   # let streams stabilise before reading their latency
AUTOALIGN_PERIOD_S = 30.0  # re-check periodically to catch Bluetooth renegotiation

logger = logging.getLogger(__name__)

VOLUME_MIN, VOLUME_MAX, VOLUME_STEP = -10.0, 10.0, 1.0
DELAY_MIN, DELAY_MAX, DELAY_STEP = 0.0, 3000.0, 10.0
STATS_INTERVAL = 0.5

TRANSLATIONS = {
    "en": {
        "source": "Capture from (where the sound is playing)", "targets": "Target Devices", "day": "Day", "night": "Night",
        "restart": "Restart", "start": "Start", "stop": "Stop", "add": "Add", "refresh": "Refresh",
        "clear": "Clear List", "delay": "Delay (ms)", "volume": "Volume (dB)", "quality": "Audio quality:",
        "ready": "Ready", "running": "Running", "stopped": "Stopped", "streams": "Streams",
        "perf": "Buffer", "errors": "Underflows",
    },
    "ru": {
        "source": "Откуда захватывать (где играет звук)", "targets": "Целевые устройства", "day": "День", "night": "Ночь",
        "restart": "Перезапустить", "start": "Запустить", "stop": "Остановить", "add": "Добавить",
        "refresh": "Обновить", "clear": "Очистить список", "delay": "Задержка (мс)",
        "volume": "Громкость (дБ)", "quality": "Настройки качества звука:", "ready": "Готов к работе",
        "running": "Работает", "stopped": "Остановлено", "streams": "Потоки", "perf": "Буфер",
        "errors": "Ошибки",
    },
}


def _to_float(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _is_feedback_loop(source_id: str | None, target_id: str) -> bool:
    """True if playing to ``target_id`` would feed back into ``source_id``.

    A capture source is a sink's monitor, named ``<sink>.monitor``. Routing the
    output to that same ``<sink>`` loops the audio back into the capture, which
    runs away into a deafening feedback howl.
    """
    if not source_id:
        return False
    return source_id.removesuffix(".monitor") == target_id


class SoundSplitterUI:
    def __init__(self, page: ft.Page, store: SettingsStore) -> None:
        self.page = page
        self.store = store
        self.settings = store.load()
        self.language = self.settings.language if self.settings.language in TRANSLATIONS else "en"
        self.dark = self.settings.theme != "light"
        self.router = AudioRouter()  # 48 kHz / 256-frame buffer — sensible defaults
        self._cfg: dict[str, TargetSettings] = {}  # per-device user trim (delay_ms) + volume
        self._cards: dict[str, dict] = {}
        self._offsets: dict[str, float] = {}  # auto-align offset per device (ms), recomputed each start
        self._autoalign = self.settings.autoalign
        # The built-in virtual cable. Created first so its monitor shows up as a
        # capture source and its sink is excluded from the output list below.
        self.vsink = VirtualSink()
        self.vsink.create()
        atexit.register(self.vsink.destroy)
        self.page.on_disconnect = lambda _=None: self.vsink.destroy()
        self._outputs = [d for d in devices.list_output_devices() if d.id != self.vsink.sink_name]
        self._stats_stop = threading.Event()
        self._setup_ui()
        self._restore()

    def _t(self, key: str) -> str:
        return TRANSLATIONS[self.language][key]

    # --- layout (mirrors the original setup_ui) --------------------------

    def _setup_ui(self) -> None:
        self.page.title = "SoundSplitter"
        self.page.padding = 20
        self.page.scroll = ft.ScrollMode.AUTO
        self.page.theme_mode = ft.ThemeMode.DARK if self.dark else ft.ThemeMode.LIGHT

        sources = [s for s in devices.list_capture_sources() if s.is_loopback]
        source_options = [
            ft.DropdownOption(key=s.id, text=s.name.removeprefix("Monitor of ").strip())
            for s in sources
            if s.id != self.vsink.monitor_source
        ]
        if self.vsink.created:
            # The virtual cable's own monitor — the natural source for alignment.
            source_options.insert(0, ft.DropdownOption(key=self.vsink.monitor_source, text="🔌 SoundSplitter (virtual cable)"))
        self.source_combo = ft.Dropdown(
            label=self._t("source"),
            options=source_options,
            on_select=self._on_source_change,
        )
        self.source_combo.expand = True

        self.virtual_switch = ft.Switch(
            label="Route current output through SoundSplitter",
            value=False,
            on_change=self._on_virtual_toggle,
            visible=self.vsink.created,
        )
        self.autoalign_switch = ft.Switch(
            label="Auto-align device latency (tracks Bluetooth; delays below are a fine trim)",
            value=self._autoalign,
            on_change=self._on_autoalign_toggle,
        )
        self.target_combo = ft.Dropdown(
            label=self._t("targets"),
            options=[ft.DropdownOption(key=d.id, text=d.name) for d in self._outputs],
        )
        self.target_combo.expand = True

        self.add_button = ft.ElevatedButton(content=self._t("add"), icon=ft.Icons.ADD, on_click=self._on_add)
        self.refresh_button = ft.ElevatedButton(content=self._t("refresh"), icon=ft.Icons.REFRESH, on_click=self._on_refresh)
        self.theme_button = ft.ElevatedButton(
            content=self._t("night") if self.dark else self._t("day"),
            icon=ft.Icons.DARK_MODE if self.dark else ft.Icons.LIGHT_MODE, on_click=self._on_theme,
        )
        self.lang_button = ft.ElevatedButton(
            content="Рус" if self.language == "ru" else "Eng", on_click=self._on_language,
        )

        self.start_button = ft.ElevatedButton(content=self._t("start"), icon=ft.Icons.PLAY_ARROW, on_click=lambda _: self._start())
        self.stop_button = ft.ElevatedButton(content=self._t("stop"), icon=ft.Icons.STOP, on_click=lambda _: self._stop(), disabled=True)
        self.restart_button = ft.ElevatedButton(content=self._t("restart"), icon=ft.Icons.REFRESH, on_click=lambda _: self._restart(), disabled=True)
        self.clear_button = ft.ElevatedButton(content=self._t("clear"), on_click=lambda _: self._clear(), visible=False)

        self.status_text = ft.Text(self._t("ready"), size=12, weight=ft.FontWeight.BOLD)
        self.streams_text = ft.Text(f"{self._t('streams')}: 0", size=12)
        self.perf_text = ft.Text(f"{self._t('perf')}: --", size=12)
        self.errors_text = ft.Text(f"{self._t('errors')}: 0", size=12)
        status_bar = ft.Container(
            content=ft.Row(
                [self.status_text, ft.VerticalDivider(width=1), self.streams_text,
                 ft.VerticalDivider(width=1), self.perf_text, ft.VerticalDivider(width=1), self.errors_text],
                spacing=15,
            ),
            border=ft.Border.all(1, ft.Colors.GREY), border_radius=5, padding=10, margin=5,
        )

        self.devices_row = ft.Row(wrap=True, spacing=10, run_spacing=10)
        self.devices_panel = ft.Container(
            content=self.devices_row, border=ft.Border.all(2, ft.Colors.BLUE),
            padding=15, margin=15, border_radius=15, visible=False,
        )

        self.page.add(
            ft.Container(
                padding=20,
                content=ft.Column(
                    [
                        ft.Text("🎵 Audio Forwarder", size=28, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER),
                        ft.Divider(height=20, thickness=2),
                        self.virtual_switch,
                        self.autoalign_switch,
                        self.source_combo,
                        self.target_combo,
                        ft.Row(
                            [ft.Row([self.add_button, self.refresh_button], spacing=10),
                             ft.Row([self.theme_button, self.lang_button], spacing=10)],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        ft.Row([self.start_button, self.stop_button, self.restart_button], spacing=10),
                        self.devices_panel,
                        self.clear_button,
                        status_bar,
                    ],
                    spacing=15,
                ),
            )
        )

    def _restore(self) -> None:
        if self.settings.source_id:
            self.source_combo.value = self.settings.source_id
            self.router.set_source(self.settings.source_id)
        for target in self.settings.targets:
            if _is_feedback_loop(self.settings.source_id, str(target.index)):
                logger.warning("skipping persisted feedback target: %r", target.index)
                continue
            self._add_device(str(target.index), target.name, target.delay_ms, target.volume_db)
        self.page.update()

    # --- device cards (mirrors the original add_device_to_ui) ------------

    def _add_device(self, device_id: str, name: str, delay_ms: float, volume_db: float) -> None:
        if device_id in self._cards:
            return
        self.router.add_target(device_id, name, delay_ms=delay_ms, volume_db=volume_db)
        self._cfg[device_id] = TargetSettings(index=device_id, name=name, delay_ms=delay_ms, volume_db=volume_db)

        delay_slider = ft.Slider(
            value=delay_ms, min=DELAY_MIN, max=DELAY_MAX, divisions=300, label="{value} ms", expand=True,
            on_change=lambda e, i=device_id: self._preview(i, "delay", e.control.value),
            on_change_end=lambda e, i=device_id: self._set_delay(i, e.control.value),
        )
        delay_field = ft.TextField(
            label=self._t("delay"), value=str(int(delay_ms)), width=120, text_align=ft.TextAlign.CENTER,
            border_radius=10, on_submit=lambda e, i=device_id: self._set_delay(i, _to_float(e.control.value)),
        )
        volume_slider = ft.Slider(
            value=volume_db, min=VOLUME_MIN, max=VOLUME_MAX, divisions=20, label="{value} dB", expand=True,
            on_change=lambda e, i=device_id: self._set_volume(i, e.control.value),
        )
        volume_field = ft.TextField(
            label=self._t("volume"), value=str(int(volume_db)), width=120, text_align=ft.TextAlign.CENTER,
            border_radius=10, on_submit=lambda e, i=device_id: self._set_volume(i, _to_float(e.control.value)),
        )

        def icon_btn(icon, handler, tip, color=None):
            b = ft.IconButton(icon=icon, on_click=handler)
            b.tooltip = tip
            if color is not None:
                b.icon_color = color
            return b

        remove_btn = icon_btn(
            ft.Icons.CLOSE, lambda e, i=device_id: self._on_remove(i), "Remove this device", color=ft.Colors.RED_400
        )
        d_minus = icon_btn(ft.Icons.REMOVE, lambda e, i=device_id: self._set_delay(i, self._cfg[i].delay_ms - DELAY_STEP), "Decrease delay")
        d_plus = icon_btn(ft.Icons.ADD, lambda e, i=device_id: self._set_delay(i, self._cfg[i].delay_ms + DELAY_STEP), "Increase delay")
        v_minus = icon_btn(ft.Icons.REMOVE, lambda e, i=device_id: self._set_volume(i, self._cfg[i].volume_db - VOLUME_STEP), "Decrease volume")
        v_plus = icon_btn(ft.Icons.ADD, lambda e, i=device_id: self._set_volume(i, self._cfg[i].volume_db + VOLUME_STEP), "Increase volume")

        container = ft.Container(
            width=350, padding=15, margin=5, border_radius=15, border=ft.Border.all(2, ft.Colors.BLUE),
            content=ft.Column(
                [
                    ft.Row([ft.Text(f"🔊 {name}", size=16, weight=ft.FontWeight.BOLD), remove_btn],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Row([d_minus, delay_field, d_plus], alignment=ft.MainAxisAlignment.CENTER),
                    delay_slider,
                    ft.Divider(height=10, thickness=2),
                    ft.Row([v_minus, volume_field, v_plus], alignment=ft.MainAxisAlignment.CENTER),
                    volume_slider,
                ],
                spacing=10,
            ),
        )
        self._cards[device_id] = {
            "container": container, "delay_slider": delay_slider, "delay_field": delay_field,
            "volume_slider": volume_slider, "volume_field": volume_field,
        }
        self.devices_row.controls.append(container)
        self._update_panel_visibility()

    def _preview(self, device_id: str, kind: str, value: float) -> None:
        card = self._cards.get(device_id)
        if card:
            card[f"{kind}_field"].value = str(int(value))
            self.page.update()

    def _effective_delay(self, device_id: str) -> float:
        """User's trim plus the auto-align offset (0 when auto-align is off)."""
        trim = self._cfg[device_id].delay_ms if device_id in self._cfg else 0.0
        return trim + self._offsets.get(device_id, 0.0)

    def _apply_effective_delay(self, device_id: str) -> None:
        # set_delay is seamless now, so applying this live never glitches.
        self.router.set_delay(device_id, self._effective_delay(device_id))

    def _set_delay(self, device_id: str, value: float) -> None:
        # The slider is the user's trim; the effective delay adds the auto offset.
        value = _clamp(value, DELAY_MIN, DELAY_MAX)
        if device_id in self._cfg:
            self._cfg[device_id].delay_ms = value
        self._apply_effective_delay(device_id)
        card = self._cards.get(device_id)
        if card:
            card["delay_slider"].value = value
            card["delay_field"].value = str(int(value))
        self.page.update()
        self._persist()

    def _set_volume(self, device_id: str, value: float) -> None:
        value = _clamp(value, VOLUME_MIN, VOLUME_MAX)
        self.router.set_volume(device_id, value)
        if device_id in self._cfg:
            self._cfg[device_id].volume_db = value
        card = self._cards.get(device_id)
        if card:
            card["volume_slider"].value = value
            card["volume_field"].value = str(int(value))
        self.page.update()
        self._persist()

    # --- handlers --------------------------------------------------------

    def _on_source_change(self, _: ft.ControlEvent) -> None:
        logger.info("source selected: %r", self.source_combo.value)
        if self.source_combo.value:
            self.router.set_source(self.source_combo.value)
            self._persist()

    def _on_virtual_toggle(self, e: ft.ControlEvent) -> None:
        """Point the system's output at the virtual sink (and move the audio that
        is already playing onto it), or restore the previous output. Nothing else
        — the user picks the source and targets and starts the stream manually."""
        if e.control.value:
            self.vsink.engage()
            self.status_text.value = "System output → SoundSplitter"
        else:
            self.vsink.disengage()
            self.status_text.value = "System output restored"
        self.page.update()

    def _on_add(self, _: ft.ControlEvent) -> None:
        device_id = self.target_combo.value
        if not device_id or device_id in self._cards:
            return
        if _is_feedback_loop(self.source_combo.value, device_id):
            # Routing a sink back into its own monitor feeds the output straight
            # into the capture — a runaway feedback loop that spikes the volume.
            self.status_text.value = "⚠ That device is the capture source — routing to it would create a feedback loop."
            self.page.update()
            logger.warning("blocked feedback routing: source=%r -> target=%r", self.source_combo.value, device_id)
            return
        device = next((d for d in self._outputs if d.id == device_id), None)
        if device is not None:
            logger.info("output added: %s (id=%r)", device.name, device.id)
            self._add_device(device.id, device.name, 0.0, 0.0)
            self.page.update()
            self._persist()

    def _on_remove(self, device_id: str) -> None:
        self.router.remove_target(device_id)
        self._cfg.pop(device_id, None)
        card = self._cards.pop(device_id, None)
        if card is not None:
            self.devices_row.controls.remove(card["container"])
        self._update_panel_visibility()
        self.page.update()
        self._persist()

    def _clear(self) -> None:
        for device_id in list(self._cards):
            self._on_remove(device_id)

    def _on_refresh(self, _: ft.ControlEvent) -> None:
        if self.router.running:
            return
        self._outputs = [d for d in devices.list_output_devices() if d.id != self.vsink.sink_name]
        self.target_combo.options = [ft.DropdownOption(key=d.id, text=d.name) for d in self._outputs]
        self.page.update()

    def _on_theme(self, _: ft.ControlEvent) -> None:
        self.dark = not self.dark
        self.page.theme_mode = ft.ThemeMode.DARK if self.dark else ft.ThemeMode.LIGHT
        self.theme_button.content = self._t("night") if self.dark else self._t("day")
        self.theme_button.icon = ft.Icons.DARK_MODE if self.dark else ft.Icons.LIGHT_MODE
        self.settings.theme = "dark" if self.dark else "light"
        self._persist()
        self.page.update()

    def _on_language(self, _: ft.ControlEvent) -> None:
        self.language = "ru" if self.language == "en" else "en"
        self.settings.language = self.language
        self._retranslate()
        self._persist()

    # --- start / stop ----------------------------------------------------

    def _start(self) -> None:
        try:
            self.router.start()
        except Exception as exc:
            self.status_text.value = f"Error: {exc}"
            self.page.update()
            return
        self.start_button.disabled = True
        self.stop_button.disabled = False
        self.restart_button.disabled = False
        self.status_text.value = self._t("running")
        self._stats_stop.clear()
        threading.Thread(target=self._stats_loop, name="ui-stats", daemon=True).start()
        if self._autoalign:
            threading.Thread(target=self._autoalign_loop, name="autoalign", daemon=True).start()
        self.page.update()

    def _stop(self) -> None:
        self.router.stop()
        self._stats_stop.set()
        self.start_button.disabled = False
        self.stop_button.disabled = True
        self.restart_button.disabled = True
        self.status_text.value = self._t("stopped")
        self.streams_text.value = f"{self._t('streams')}: 0"
        self.page.update()

    def _restart(self) -> None:
        self._stop()
        self._start()

    # --- auto-alignment (reported-latency tracking) ----------------------

    def _on_autoalign_toggle(self, e: ft.ControlEvent) -> None:
        self._autoalign = bool(e.control.value)
        self.settings.autoalign = self._autoalign
        if not self._autoalign:
            # Drop the offsets so delays fall back to the raw user trim.
            self._offsets = {}
            for device_id in self._cards:
                self._apply_effective_delay(device_id)
        elif self.router.running:
            threading.Thread(target=self._autoalign_loop, name="autoalign", daemon=True).start()
        self._persist()
        self.page.update()

    def _autoalign_loop(self) -> None:
        # Let the streams settle, then read each device's real latency and align;
        # repeat periodically so a Bluetooth renegotiation is tracked, not re-tuned.
        if self._stats_stop.wait(AUTOALIGN_SETTLE_S):
            return
        while self._autoalign and self.router.running:
            ids = list(self._cards)
            measured = latency.read_latencies_ms(ids, self.router.samplerate)
            if measured:
                offsets = latency.align_offsets(measured)
                self._offsets = offsets
                for device_id in ids:
                    self._apply_effective_delay(device_id)
                summary = ", ".join(
                    f"{self._cfg[i].name.split()[0]} +{int(offsets.get(i, 0))}"
                    for i in ids if i in self._cfg
                )
                self.status_text.value = f"{self._t('running')} · auto-aligned: {summary} ms"
                try:
                    self.page.update()
                except Exception:
                    break
            if self._stats_stop.wait(AUTOALIGN_PERIOD_S):
                break

    # --- live status -----------------------------------------------------

    def _stats_loop(self) -> None:
        while not self._stats_stop.wait(STATS_INTERVAL):
            stats = self.router.get_stats()
            if self.router.error is not None:
                self.status_text.value = f"Error: {self.router.error}"
                self._stats_stop.set()
            else:
                underflows = sum(int(s["underflows"]) for s in stats)
                buffered = max((float(s["buffered_ms"]) for s in stats), default=0.0)
                self.streams_text.value = f"{self._t('streams')}: {len(stats)}"
                self.perf_text.value = f"{self._t('perf')}: {buffered:.0f} ms"
                self.errors_text.value = f"{self._t('errors')}: {underflows}"
            try:
                self.page.update()
            except Exception:
                break

    # --- misc ------------------------------------------------------------

    def _update_panel_visibility(self) -> None:
        visible = bool(self._cards)
        self.devices_panel.visible = visible
        self.clear_button.visible = visible

    def _retranslate(self) -> None:
        self.source_combo.label = self._t("source")
        self.target_combo.label = self._t("targets")
        self.add_button.content = self._t("add")
        self.refresh_button.content = self._t("refresh")
        self.theme_button.content = self._t("night") if self.dark else self._t("day")
        self.lang_button.content = "Рус" if self.language == "ru" else "Eng"
        self.start_button.content = self._t("start")
        self.stop_button.content = self._t("stop")
        self.restart_button.content = self._t("restart")
        self.clear_button.content = self._t("clear")
        self.page.update()

    def _persist(self) -> None:
        self.settings.source_id = self.source_combo.value
        self.settings.autoalign = self._autoalign
        self.settings.targets = list(self._cfg.values())
        try:
            self.store.save(self.settings)
        except OSError:
            logger.exception("failed to save settings")


def build(page: ft.Page) -> None:
    """Flet entry point used by ``ft.app(target=build)``."""
    SoundSplitterUI(page, SettingsStore())
