"""Double-click launcher for the existing local review application."""
import getpass
import os
from pathlib import Path
import webbrowser

from werkzeug.serving import make_server

from multi_hazard_pipeline.config import DEFAULT_CONFIG, PROVIDERS, setting_from_env
from multi_hazard_pipeline.web import create_app


def main():
    # The default provider first; the other one can be chosen per run in the interface.
    order = [DEFAULT_CONFIG.llm.provider] + [key for key in PROVIDERS if key != DEFAULT_CONFIG.llm.provider]
    for provider in order:
        key_name = PROVIDERS[provider]["api_key_env_var"]
        if setting_from_env(key_name):
            continue
        default = " (default provider)" if provider == DEFAULT_CONFIG.llm.provider else " (optional)"
        print()
        print(f"{PROVIDERS[provider]['label']}{default}: enter its API key ({key_name}), or press Enter to skip.")
        key = getpass.getpass("API key (hidden; used only for this session): ").strip()
        if key:
            os.environ[key_name] = key
    app = create_app(Path(__file__).resolve().parent / "results")
    # Let Windows select a free port, so another app cannot block startup.
    server = make_server("127.0.0.1", 0, app, threaded=True)
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"\nMulti-hazard review: {address}\nKeep this window open. Press Ctrl+C to stop.")
    webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
