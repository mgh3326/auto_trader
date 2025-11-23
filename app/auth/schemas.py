"""Pydantic schemas for authentication."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class Token(BaseModel):
    """Token response schema."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    """Data extracted from JWT token."""

    username: Optional[str] = None


class UserCreate(BaseModel):
    """Schema for creating a new user."""

    email: EmailStr
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """비밀번호 강도 검증: 최소 8자, 대문자, 숫자 포함."""
        if len(v) < 8:
            raise ValueError("비밀번호는 최소 8자 이상이어야 합니다.")

        if not any(c.isupper() for c in v):
            raise ValueError("비밀번호에 대문자가 최소 1개 이상 포함되어야 합니다.")

        if not any(c.isdigit() for c in v):
            raise ValueError("비밀번호에 숫자가 최소 1개 이상 포함되어야 합니다.")

        return v


class UserInDB(BaseModel):
    """User schema for database representation."""

    id: int
    email: str
    username: str
    is_active: bool

    class Config:
        from_attributes = True


class UserResponse(BaseModel):
    """User response schema (excludes sensitive data)."""

    id: int
    email: str
    username: str
    is_active: bool

    class Config:
        from_attributes = True


class RefreshTokenRequest(BaseModel):
    """Schema for refresh token request."""

    refresh_token: str
