"""Double-click launcher for the existing local review application."""
import getpass
import os
from pathlib import Path
import webbrowser

from werkzeug.serving import make_server

from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.web import create_app


def main():
    key_name = DEFAULT_CONFIG.llm.api_key_env_var
    if not os.environ.get(key_name, "").strip():
        print("Enter your LLM API key to process reports, or press Enter to browse existing results.")
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
