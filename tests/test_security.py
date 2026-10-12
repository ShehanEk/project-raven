from raven import security
from raven.config import HostSettings, SettingsStore


def test_lan_addresses():
    for ok in ["127.0.0.1", "10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.1.20",
               "169.254.3.4", "::1", "fe80::1%en0", "fd12:3456::1", "::ffff:192.168.0.5"]:
        assert security.is_lan_address(ok), ok
    for bad in ["8.8.8.8", "172.32.0.1", "100.64.1.1", "2001:4860::8888", "::ffff:8.8.8.8", "nonsense", ""]:
        assert not security.is_lan_address(bad), bad


def test_proof_round_trip():
    host = HostSettings()
    host.set_password("correct horse")
    nonce = security.new_nonce()
    fp = "AA:BB"
    client_key = security.derive_key("correct horse", bytes.fromhex(host.password_salt))
    proof = security.make_proof(client_key, nonce, fp)
    verifier = bytes.fromhex(host.password_verifier)
    assert security.verify_proof(verifier, nonce, fp, proof)
    assert not security.verify_proof(verifier, nonce, "AA:BC", proof)  # other TLS channel
    assert not security.verify_proof(verifier, security.new_nonce(), fp, proof)  # replay
    wrong = security.make_proof(security.derive_key("nope", bytes.fromhex(host.password_salt)), nonce, fp)
    assert not security.verify_proof(verifier, nonce, fp, wrong)


def test_certificate_is_stable(tmp_path):
    _, _, fp1 = security.ensure_certificate(tmp_path)
    _, _, fp2 = security.ensure_certificate(tmp_path)
    assert fp1 == fp2 and len(fp1) == 95


def test_rate_limiter():
    limiter = security.AuthRateLimiter(max_failures=3, lockout_seconds=60)
    for _ in range(3):
        assert not limiter.is_locked("a")
        limiter.record_failure("a")
    assert limiter.is_locked("a")
    assert not limiter.is_locked("b")


def test_settings_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    store.settings.host.set_password("secret123")
    store.settings.host.port = 5000
    store.save()
    again = SettingsStore(tmp_path / "s.json")
    assert again.settings.host.port == 5000
    assert again.settings.host.has_password
