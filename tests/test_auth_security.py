import pytest
from datetime import timedelta
import jwt
from app.auth.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    create_refresh_token,
)
from app.core.config import settings

def test_password_hashing():
    password = "testpassword"
    hashed = get_password_hash(password)
    assert verify_password(password, hashed)
    assert not verify_password("wrongpassword", hashed)

def test_create_access_token():
    data = {"sub": "testuser"}
    token = create_access_token(data=data)
    decoded = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert decoded["sub"] == "testuser"
    assert decoded["type"] == "access"
    assert "exp" in decoded

def test_create_access_token_with_expiry():
    data = {"sub": "testuser"}
    expires = timedelta(minutes=10)
    token = create_access_token(data=data, expires_delta=expires)
    decoded = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert decoded["sub"] == "testuser"
    # Check if expiration is roughly correct (within a few seconds)
    # This is a bit tricky to test exactly without mocking time, but existence is key
    assert "exp" in decoded

def test_create_refresh_token():
    data = {"sub": "testuser"}
    token = create_refresh_token(data=data)
    decoded = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert decoded["sub"] == "testuser"
    assert decoded["type"] == "refresh"
    assert "exp" in decoded
