"""Admin routes need a BetterAuth JWT whose signature actually verifies.

get_admin_user used to base64-decode the payload and trust it, so anyone
who knew an admin's user id could forge admin access to every admin route.
"""
import base64
import json
import pathlib
import sys
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import dependencies
from database.models import Base, User


def _jwk(private):
    return json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(private.public_key()))


@pytest.fixture()
def keys():
    return {"real": Ed25519PrivateKey.generate(), "stranger": Ed25519PrivateKey.generate()}


@pytest.fixture()
def db(keys):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__])
    s = sessionmaker(bind=engine)()
    s.execute(text("CREATE TABLE jwks (id TEXT, public_key TEXT, private_key TEXT, "
                   "created_at TIMESTAMP, expires_at TIMESTAMP)"))
    s.execute(text("INSERT INTO jwks (id, public_key) VALUES ('kid1', :pk)"),
              {"pk": json.dumps(_jwk(keys["real"]))})
    from datetime import datetime
    s.add_all([
        User(id="admin1", email="a@x", name="A", email_verified=True, role="admin",
             created_at=datetime.utcnow(), updated_at=datetime.utcnow()),
        User(id="user1", email="u@x", name="U", email_verified=True, role="user",
             created_at=datetime.utcnow(), updated_at=datetime.utcnow()),
    ])
    s.commit()
    dependencies._admin_keys_cache.update(at=0.0, keys=[])
    # SQLite has no NOW(); the production query uses it.
    s.connection().connection.create_function("NOW", 0, lambda: "9999-12-31")
    yield s
    s.close()


def _req(token):
    return Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})


def _token(private, sub="admin1", exp_in=600, kid="kid1"):
    return jwt.encode({"sub": sub, "exp": int(time.time()) + exp_in}, private,
                      algorithm="EdDSA", headers={"kid": kid})


def test_signed_admin_token_is_accepted(db, keys):
    assert dependencies.get_admin_user(_req(_token(keys["real"])), db).id == "admin1"


def test_token_signed_by_another_key_is_refused(db, keys):
    with pytest.raises(HTTPException) as exc:
        dependencies.get_admin_user(_req(_token(keys["stranger"])), db)
    assert exc.value.status_code == 401


def test_hand_written_token_is_refused(db):
    # Exactly what used to work: a made-up payload with a junk signature.
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    forged = f"{b64({'alg': 'EdDSA', 'kid': 'kid1'})}.{b64({'sub': 'admin1', 'exp': int(time.time()) + 600})}.AAAA"
    with pytest.raises(HTTPException) as exc:
        dependencies.get_admin_user(_req(forged), db)
    assert exc.value.status_code == 401


def test_alg_none_is_refused(db):
    forged = jwt.encode({"sub": "admin1", "exp": int(time.time()) + 600}, None, algorithm="none")
    with pytest.raises(HTTPException):
        dependencies.get_admin_user(_req(forged), db)


def test_expired_token_is_refused(db, keys):
    with pytest.raises(HTTPException) as exc:
        dependencies.get_admin_user(_req(_token(keys["real"], exp_in=-10)), db)
    assert exc.value.status_code == 401


def test_valid_token_for_a_non_admin_is_403(db, keys):
    with pytest.raises(HTTPException) as exc:
        dependencies.get_admin_user(_req(_token(keys["real"], sub="user1")), db)
    assert exc.value.status_code == 403


def test_unknown_kid_still_verifies_against_current_keys(db, keys):
    assert dependencies.get_admin_user(_req(_token(keys["real"], kid="rotated")), db).id == "admin1"
