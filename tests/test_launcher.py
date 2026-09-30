from unittest.mock import Mock

import launch_interface


def test_launcher_uses_local_free_port_and_closes_server(monkeypatch):
    monkeypatch.setenv("TW_LLM_API_KEY", "test-only")
    app = Mock()
    server = Mock(server_port=54321)
    server.serve_forever.side_effect = KeyboardInterrupt
    create = Mock(return_value=app)
    make = Mock(return_value=server)
    browser = Mock()
    monkeypatch.setattr(launch_interface, "create_app", create)
    monkeypatch.setattr(launch_interface, "make_server", make)
    monkeypatch.setattr(launch_interface.webbrowser, "open", browser)
    launch_interface.main()
    create.assert_called_once_with(launch_interface.Path(launch_interface.__file__).resolve().parent / "results")
    make.assert_called_once_with("127.0.0.1", 0, app, threaded=True)
    browser.assert_called_once_with("http://127.0.0.1:54321")
    server.server_close.assert_called_once()


def test_cli_defaults_to_results():
    from multi_hazard_pipeline.cli import parser

    assert parser().parse_args(["run", "input"]).output_dir == "results"
    assert parser().parse_args(["serve"]).output_dir == "results"
    assert parser().parse_args(["split", "input.pdf"]).output_dir == "results/split"
    assert parser().parse_args(["run", "input", "custom"]).output_dir == "custom"
