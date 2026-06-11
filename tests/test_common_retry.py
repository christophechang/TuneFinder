"""Tests for bounded retry logic in src/fetchers/common.py."""
from unittest.mock import patch, MagicMock, call
import pytest
import requests

from src.fetchers.common import get_html, post_html


def _timeout_response():
    raise requests.Timeout("timed out")


def _ok_response(text="<html>ok</html>"):
    mock = MagicMock()
    mock.text = text
    mock.raise_for_status = MagicMock()
    return mock


def _http_error_response(status: int):
    resp = MagicMock()
    resp.status_code = status
    err = requests.HTTPError(response=resp)
    mock = MagicMock()
    mock.raise_for_status.side_effect = err
    return mock


def _connection_error():
    raise requests.ConnectionError("connection refused")


# ---------------------------------------------------------------------------
# GET tests
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("requests.request")
def test_get_succeeds_after_one_timeout(mock_request, mock_sleep):
    mock_request.side_effect = [requests.Timeout("t"), _ok_response()]
    result = get_html("http://example.com")
    assert result == "<html>ok</html>"
    assert mock_request.call_count == 2
    assert mock_sleep.call_count == 1


@patch("time.sleep")
@patch("requests.request")
def test_get_500_then_200(mock_request, mock_sleep):
    mock_request.side_effect = [_http_error_response(500), _ok_response()]
    result = get_html("http://example.com")
    assert result == "<html>ok</html>"
    assert mock_request.call_count == 2
    assert mock_sleep.call_count == 1


@patch("time.sleep")
@patch("requests.request")
def test_get_403_raises_immediately_one_call(mock_request, mock_sleep):
    mock_request.return_value = _http_error_response(403)
    with pytest.raises(requests.HTTPError):
        get_html("http://example.com")
    assert mock_request.call_count == 1
    mock_sleep.assert_not_called()


@patch("time.sleep")
@patch("requests.request")
def test_get_three_timeouts_exhausts_and_raises(mock_request, mock_sleep):
    mock_request.side_effect = requests.Timeout("always")
    with pytest.raises(requests.Timeout):
        get_html("http://example.com")
    assert mock_request.call_count == 3
    assert mock_sleep.call_count == 2  # before attempt 2 and 3


@patch("time.sleep")
@patch("requests.request")
def test_get_429_retries(mock_request, mock_sleep):
    mock_request.side_effect = [_http_error_response(429), _ok_response()]
    result = get_html("http://example.com")
    assert result == "<html>ok</html>"
    assert mock_request.call_count == 2


# ---------------------------------------------------------------------------
# POST tests
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("requests.request")
def test_post_succeeds_after_timeout(mock_request, mock_sleep):
    mock_request.side_effect = [requests.Timeout("t"), _ok_response("data")]
    result = post_html("http://example.com", data={"q": "1"})
    assert result == "data"
    assert mock_request.call_count == 2
    assert mock_sleep.call_count == 1


@patch("time.sleep")
@patch("requests.request")
def test_post_404_raises_immediately(mock_request, mock_sleep):
    mock_request.return_value = _http_error_response(404)
    with pytest.raises(requests.HTTPError):
        post_html("http://example.com", data={})
    assert mock_request.call_count == 1
    mock_sleep.assert_not_called()
