"""P1-1 安全加固单元测试：鉴权、SSRF、CORS、错误隐藏。"""
import os
import sys
import pytest
from importlib import reload

sys.path.insert(0, os.path.dirname(__file__))
import security


class TestAuth:
    def test_no_token_dev_mode_allows(self, monkeypatch):
        monkeypatch.setattr(security, "READER_API_TOKEN", "")
        assert security.check_token("") is True
        assert security.check_token("anything") is True

    def test_token_required(self, monkeypatch):
        monkeypatch.setattr(security, "READER_API_TOKEN", "secret123")
        assert security.check_token("secret123") is True
        assert security.check_token("wrong") is False
        assert security.check_token("") is False
        assert security.check_token(None) is False


class TestBaseUrlValidation:
    def test_blocks_metadata_ip(self, monkeypatch):
        monkeypatch.setattr(security, "ALLOWED_BASE_URLS", "")
        err = security.validate_base_url("http://169.254.169.254/")
        assert err

    def test_blocks_private_ips(self, monkeypatch):
        monkeypatch.setattr(security, "ALLOWED_BASE_URLS", "")
        for url in [
            "http://192.168.1.1/",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://127.0.0.1/",
            "http://localhost:8321/",
        ]:
            assert security.validate_base_url(url), f"expected block: {url}"

    def test_blocks_http_and_non_https(self, monkeypatch):
        monkeypatch.setattr(security, "ALLOWED_BASE_URLS", "")
        assert security.validate_base_url("http://example.com/")
        assert security.validate_base_url("ftp://example.com/")

    def test_allows_https_public(self, monkeypatch):
        monkeypatch.setattr(security, "ALLOWED_BASE_URLS", "")
        assert security.validate_base_url("https://api.openai.com/v1") is None
        assert security.validate_base_url("https://example.com/path") is None

    def test_whitelist_mode(self, monkeypatch):
        monkeypatch.setattr(security, "ALLOWED_BASE_URLS", "https://api.example.com,https://api2.example.com")
        assert security.validate_base_url("https://api.example.com") is None
        assert security.validate_base_url("https://api.example.com/") is None
        assert security.validate_base_url("https://api2.example.com") is None
        assert security.validate_base_url("https://api.openai.com/v1")
        assert security.validate_base_url("http://api.example.com")


class TestStartup:
    def test_loopback_without_token_ok(self):
        security.validate_startup("127.0.0.1")

    def test_loopback_localhost_without_token_ok(self):
        security.validate_startup("localhost")

    def test_production_without_token_fails(self):
        with pytest.raises(RuntimeError):
            security.validate_startup("0.0.0.0")

    def test_production_with_token_ok(self, monkeypatch):
        monkeypatch.setattr(security, "READER_API_TOKEN", "secret")
        security.validate_startup("0.0.0.0")


class TestCors:
    def test_default_origins(self, monkeypatch):
        monkeypatch.setattr(security, "CORS_ORIGINS", "")
        origins = security.get_cors_origins()
        assert "http://localhost:5173" in origins
        assert "*" not in origins

    def test_env_origins(self, monkeypatch):
        monkeypatch.setattr(security, "CORS_ORIGINS", "https://app.example.com,https://admin.example.com")
        origins = security.get_cors_origins()
        assert origins == ["https://app.example.com", "https://admin.example.com"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
