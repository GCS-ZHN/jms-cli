# -*- coding: utf-8 -*-
"""Tests for jms.config."""

import os
import stat

import pytest
import yaml

from jms.config import (
    AppConfig,
    ServerConfig,
    add_server,
    load_config,
    remove_server,
    save_config,
    set_default_server,
)
from jms.exceptions import ConfigError


def _save(path, **srv_kw) -> None:
    kw = {"host": "jump.example.com", "username": "alice", "password": "pw"}
    kw.update(srv_kw)
    cfg = AppConfig(
        default="prod",
        servers={"prod": ServerConfig(name="prod", **kw)},
    )
    save_config(cfg, str(path))


def test_save_load_round_trip(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    _save(p, otp_secret="BASE32SECRET")
    cfg = load_config(str(p))
    srv = cfg.servers["prod"]
    assert srv.password == "pw"
    assert srv.otp_secret == "BASE32SECRET"
    assert cfg.default == "prod"

    # on-disk values are encrypted
    raw = yaml.safe_load(p.read_text())
    assert raw["servers"]["prod"]["password"].startswith("enc:v1:")
    assert raw["servers"]["prod"]["otp_secret"].startswith("enc:v1:")


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permission bits are meaningless on Windows (ACLs govern)",
)
def test_file_permissions_0600(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    _save(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_save_config_without_fchmod(tmp_path, monkeypatch) -> None:
    """Platforms without os.fchmod (Windows) still save config fine."""
    monkeypatch.delattr("os.fchmod", raising=False)
    p = tmp_path / "config.yaml"
    _save(p)
    assert load_config(str(p)).servers["prod"].password == "pw"


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(ConfigError, match="Config not found"):
        load_config(str(tmp_path / "nope.yaml"))


def test_empty_file_raises(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load_config(str(p))


@pytest.mark.parametrize("field", ["host", "username", "password"])
def test_missing_required_field_raises(tmp_path, field) -> None:
    p = tmp_path / "config.yaml"
    srv = {"host": "h", "username": "u", "password": "p"}
    del srv[field]
    p.write_text(yaml.safe_dump({"servers": {"prod": srv}}))
    with pytest.raises(ConfigError, match=f"missing '{field}'"):
        load_config(str(p))


def test_null_otp_secret_ok(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "servers": {"prod": {"host": "h", "username": "u", "password": "p",
                             "otp_secret": None}},
    }))
    assert load_config(str(p)).servers["prod"].otp_secret == ""


def test_broken_yaml_not_overwritten_by_add(tmp_path) -> None:
    """add_server must propagate ConfigError, never clobber existing config."""
    p = tmp_path / "config.yaml"
    p.write_text("servers: {broken yaml!!")
    original = p.read_text()
    with pytest.raises(ConfigError):
        add_server("new", "h", "u", "p", config_path=str(p))
    assert p.read_text() == original


def test_add_server_first_becomes_default(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    add_server("prod", "h", "u", "pw", config_path=str(p))
    cfg = load_config(str(p))
    assert cfg.default == "prod"
    assert cfg.servers["prod"].password == "pw"


def test_add_server_keeps_existing(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    add_server("a", "h1", "u1", "p1", config_path=str(p))
    add_server("b", "h2", "u2", "p2", config_path=str(p))
    cfg = load_config(str(p))
    assert set(cfg.servers) == {"a", "b"}
    assert cfg.default == "a"  # first stays default


def test_remove_server_switches_default(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    add_server("a", "h1", "u1", "p1", config_path=str(p))
    add_server("b", "h2", "u2", "p2", config_path=str(p))
    remove_server("a", config_path=str(p))
    cfg = load_config(str(p))
    assert set(cfg.servers) == {"b"}
    assert cfg.default_server.name == "b"


def test_set_default_server(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    add_server("a", "h1", "u1", "p1", config_path=str(p))
    add_server("b", "h2", "u2", "p2", config_path=str(p))
    set_default_server("b", config_path=str(p))
    assert load_config(str(p)).default == "b"
    with pytest.raises(ConfigError):
        set_default_server("nope", config_path=str(p))


def test_base_url_derivation() -> None:
    bare = ServerConfig(name="x", host="jump.example.com", username="u", password="p")
    assert bare.base_url == "https://jump.example.com"
    full = ServerConfig(name="x", host="http://10.0.0.1:8080/", username="u", password="p")
    assert full.base_url == "http://10.0.0.1:8080"


def test_plaintext_password_accepted(tmp_path) -> None:
    """Unencrypted (legacy/handwritten) passwords load as-is."""
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "servers": {"prod": {"host": "h", "username": "u", "password": "plainpw"}},
    }))
    assert load_config(str(p)).servers["prod"].password == "plainpw"


def test_tampered_password_raises_config_error(tmp_path) -> None:
    """Decryption failure surfaces as ConfigError, not bare ValueError."""
    p = tmp_path / "config.yaml"
    _save(p)
    raw = yaml.safe_load(p.read_text())
    raw["servers"]["prod"]["password"] = raw["servers"]["prod"]["password"][:-4] + "AAAA"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="decryption failed"):
        load_config(str(p))
