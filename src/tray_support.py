"""System tray helper using pystray and Pillow."""

from __future__ import annotations

import threading
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw


def _make_tray_image() -> Image.Image:
    """Build a 64x64 tray image: blue rounded plate + white circle (mic hint)."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 4
    blue = (37, 99, 235, 255)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=14,
        fill=blue,
    )
    cx, cy = size // 2, size // 2
    r = 15
    draw.ellipse(
        [cx - r, cy - r, cx + r, cy + r],
        fill=(255, 255, 255, 255),
    )
    return img


class TrayIcon:
    """Tray icon with Show / Hide / Quit menu.

    Menu actions run on pystray's thread, not the Tk (or other UI) main thread.
    The **caller** should either schedule UI work with ``root.after(0, ...)``
    inside these callables, or use a ``queue.Queue`` and drain it on the main
    loop. A common pattern is: pass lambdas that call ``root.after(0, actual_handler)``.
    """

    def __init__(
        self,
        on_show: Callable[[], None],
        on_hide: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_show = on_show
        self._on_hide = on_hide
        self._on_quit = on_quit
        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None
        self._image = _make_tray_image()

    def _menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                "Show window",
                lambda _icon, _item: self._on_show(),
            ),
            pystray.MenuItem(
                "Hide to tray",
                lambda _icon, _item: self._on_hide(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Quit",
                lambda _icon, _item: self._on_quit(),
            ),
        )

    def start(self) -> None:
        """Run the tray icon in a daemon thread (``icon.run()`` blocks there)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._icon = pystray.Icon(
            "transcript_tray",
            self._image,
            menu=self._menu(),
        )
        assert self._icon is not None
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the tray icon if it was started; safe if not started."""
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            pass
        finally:
            self._icon = None
