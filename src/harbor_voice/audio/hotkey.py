"""Global hold-key listener with physical press/release de-duplication."""

from __future__ import annotations

from collections.abc import Callable


def _default_listener_factory(on_press, on_release):
    from pynput import keyboard

    return keyboard.Listener(on_press=on_press, on_release=on_release)


def _key_name(key) -> str:
    name = getattr(key, "name", None)
    if name:
        return str(name).casefold()
    text = str(key).casefold()
    if text.startswith("key."):
        return text[4:]
    return text.strip("'")


class GlobalHoldKey:
    def __init__(
        self,
        key_name: str,
        on_pressed: Callable[[str], None],
        on_released: Callable[[str], None],
        *,
        listener_factory=_default_listener_factory,
    ) -> None:
        self.key_name = key_name.casefold()
        self._on_pressed_callback = on_pressed
        self._on_released_callback = on_released
        self._listener_factory = listener_factory
        self._listener = None
        self._held = False

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = self._listener_factory(self._on_press, self._on_release)
        self._listener.start()

    def stop(self) -> None:
        if self._listener is None:
            return
        self._listener.stop()
        self._listener = None
        self._held = False

    def _on_press(self, key) -> None:
        if _key_name(key) != self.key_name or self._held:
            return
        self._held = True
        self._on_pressed_callback("pressed")

    def _on_release(self, key) -> None:
        if _key_name(key) != self.key_name or not self._held:
            return
        self._held = False
        self._on_released_callback("released")

