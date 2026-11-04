"""Entry point: start the UI and stream servers, Soloist and the bridges."""
from __future__ import annotations

import os
import signal
import threading

from . import bridge, state, web

UI_PORT = int(os.environ.get("WAM_UI_PORT", "8098"))


def main() -> None:
    opts = state.options()
    dev = os.environ.get("WAM_DEV") == "1"
    manager = bridge.BridgeManager(state, opts)
    app = web.App(manager, dev=dev)
    web.stream_server(manager, manager.stream_port)
    web.ui_server(app, UI_PORT)
    print(f"UI on port {UI_PORT}, speaker streams on port {manager.stream_port}"
          + (" (dev mode: UI open to all)" if dev else ""), flush=True)

    def startup():
        manager.prepare_soloist()
        manager.load()
        try:
            app.scan()
        except Exception as exc:  # noqa: BLE001
            print(f"initial speaker search failed: {exc}", flush=True)
        manager.start_all()
    threading.Thread(target=startup, daemon=True).start()

    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())
    done.wait()
    print("stopping", flush=True)
    manager.stop_all()


if __name__ == "__main__":
    main()
