
from backend.auth import _hash_password, _verify_password

def test_password_hash_is_not_plaintext():
    hashed=_hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert hashed.startswith("pbkdf2_sha256$")
    assert _verify_password("correct horse battery staple",hashed)
    assert not _verify_password("wrong password",hashed)
