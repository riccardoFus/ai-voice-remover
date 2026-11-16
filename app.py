#!/usr/bin/env python3
"""Avvio di AI Voice Remover.

Uso:
    python app.py            # avvia il server
    ./run.sh                 # launcher consigliato (controlla le dipendenze)

Il server è pronto anche per essere servito da un WSGI server:
    waitress-serve --port=8791 app:app
"""

from __future__ import annotations

import atexit
import os
import signal
import sys

from avr import create_app
from avr.config import (
    APP_NAME,
    APP_VERSION,
    DENOISE_MODEL_LABEL,
    MODELS,
    denoise_model_cached,
    model_is_cached,
)

app = create_app()


def _pid_file(settings):
    return settings.data_dir / "server.pid"


def _write_pid_file(settings) -> None:
    path = _pid_file(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return

    def _cleanup() -> None:
        try:
            if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass

    atexit.register(_cleanup)


def _handle_sigterm(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def _print_banner(settings) -> None:
    cached = model_is_cached(settings.default_model)
    model_label = MODELS[settings.default_model]["label"]
    if settings.denoise_available:
        denoise_state = "pronto" if denoise_model_cached() else "download al primo uso"
        denoise_label = f"{DENOISE_MODEL_LABEL} ({denoise_state})"
    else:
        denoise_label = "non installato (./install.sh --denoise)"
    lines = [
        "",
        f"  {APP_NAME} v{APP_VERSION}",
        f"  Dispositivo : {settings.device_label}",
        f"  Modello     : {model_label} ({'pronto' if cached else 'download al primo uso'})",
        f"  Denoise AI  : {denoise_label}",
        f"  Interfaccia : http://{settings.host}:{settings.port}",
        f"  Dati        : {settings.outputs_dir}",
        f"  Pulizia     : i file scadono dopo {settings.retention_hours:g} ore",
        "",
        "  Premi Ctrl+C per fermare il server.",
        "",
    ]
    print("\n".join(lines), flush=True)


def main() -> int:
    settings = app.config["AVR_SETTINGS"]
    _write_pid_file(settings)

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (AttributeError, ValueError):
        pass

    _print_banner(settings)

    try:
        from waitress import serve
    except ImportError:
        app.run(host=settings.host, port=settings.port, threaded=True, debug=False)
        return 0

    try:
        serve(
            app,
            host=settings.host,
            port=settings.port,
            threads=8,
            channel_timeout=300,
            ident=None,
            clear_untrusted_proxy_headers=True,
        )
    except OSError as exc:
        print(
            f"\nImpossibile avviare il server su {settings.host}:{settings.port}: {exc}\n"
            "Forse la porta è già occupata. Prova con AVR_PORT=8799 ./run.sh\n",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
