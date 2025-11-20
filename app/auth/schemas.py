"""Pydantic schemas for authentication."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


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
