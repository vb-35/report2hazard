"""The deterministic suite must never contact a model service or other network."""
import socket
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Network access is disabled in the deterministic test suite")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
