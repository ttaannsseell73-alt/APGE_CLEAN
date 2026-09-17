import pytest

from apge.binance_adapter import BinanceAdapter


class StreamTransport:
    def __init__(self):
        self.requests = []
        self.responses = []

    def get(self, url, params=None, headers=None):
        self.requests.append(("GET", url, params, headers))
        return self.responses.pop(0) if self.responses else {}

    def post(self, url, data=None, headers=None):
        self.requests.append(("POST", url, data, headers))
        return self.responses.pop(0) if self.responses else {}

    def put(self, url, data=None, headers=None):
        self.requests.append(("PUT", url, data, headers))
        return self.responses.pop(0) if self.responses else {}

    def delete(self, url, params=None, headers=None):
        self.requests.append(("DELETE", url, params, headers))
        return self.responses.pop(0) if self.responses else {}


def _adapter(transport, api_key="test-key"):
    return BinanceAdapter(
        transport,
        http_url="https://testnet.binancefuture.com",
        ws_url="wss://fstream.binancefuture.com",
        api_key=api_key,
        api_secret="test-secret",
    )


def test_user_data_stream_lifecycle_is_testnet_and_api_key_authenticated():
    transport = StreamTransport()
    transport.responses.extend([{"listenKey": "abc123"}, {}, {}])
    adapter = _adapter(transport)

    key = adapter.start_user_data_stream()
    adapter.keepalive_user_data_stream(key)
    adapter.close_user_data_stream(key)

    assert key == "abc123"
    assert [request[0] for request in transport.requests] == ["POST", "PUT", "DELETE"]
    assert all(
        request[1] == "https://testnet.binancefuture.com/fapi/v1/listenKey"
        for request in transport.requests
    )
    assert all(request[3]["X-MBX-APIKEY"] == "test-key" for request in transport.requests)
    assert transport.requests[1][2] == {"listenKey": "abc123"}
    assert transport.requests[2][2] == {"listenKey": "abc123"}


def test_user_data_stream_fails_closed_on_missing_credentials_or_bad_listen_key():
    with pytest.raises(ValueError):
        _adapter(StreamTransport(), api_key="").start_user_data_stream()

    transport = StreamTransport()
    transport.responses.append({})
    with pytest.raises(RuntimeError):
        _adapter(transport).start_user_data_stream()

    adapter = _adapter(StreamTransport())
    for bad in ("", "has space", "x" * 257):
        with pytest.raises(ValueError):
            adapter.keepalive_user_data_stream(bad)
