"""Stable account identity across every retained HMAC key version."""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine


class AccountIdentityError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class KeyMaterial:
    version: int
    key_id: str
    key: bytes = field(repr=False)

    @classmethod
    def from_root_secret(
        cls, version: int, key_id: str, root_secret: bytes
    ) -> KeyMaterial:
        """HKDF-SHA256 domain separation for the retained account-binding key."""

        if (
            type(version) is not int
            or version <= 0
            or type(key_id) is not str
            or not key_id
        ):
            raise AccountIdentityError("key_mismatch")
        if type(root_secret) is not bytes or len(root_secret) < 32:
            raise AccountIdentityError("key_version_unavailable")
        extract = hmac.new(b"\x00" * 32, root_secret, hashlib.sha256).digest()
        info = f"nhplug-account-binding-v{version}".encode("ascii")
        derived = hmac.new(extract, info + b"\x01", hashlib.sha256).digest()
        return cls(version, key_id, derived)

    def check(self) -> str:
        return hmac.new(self.key, b"nhplug-key-check-v1", hashlib.sha256).hexdigest()

    def binding(self, act_no: str) -> str:
        return hmac.new(self.key, act_no.encode("utf-8"), hashlib.sha256).hexdigest()


async def load_retained_keys_from_env(engine: AsyncEngine) -> dict[int, KeyMaterial]:
    """Load every database-retained version; never log key bytes or checks."""

    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT key_version,key_id FROM review.nhplug_mock_key_version ORDER BY key_version"
                    )
                )
            )
            .mappings()
            .all()
        )
    if not rows:
        raise AccountIdentityError("key_registry_empty")
    keys: dict[int, KeyMaterial] = {}
    for row in rows:
        version = row["key_version"]
        raw = os.getenv(f"NHPLUG_STAGE2_ROOT_SECRET_V{version}")
        if raw is None:
            raise AccountIdentityError("key_version_unavailable")
        keys[version] = KeyMaterial.from_root_secret(
            version, row["key_id"], raw.encode("utf-8")
        )
    return keys


async def resolve_account_ref(
    engine: AsyncEngine,
    act_no: str,
    keys: dict[int, KeyMaterial],
    *,
    create: bool = True,
) -> UUID:
    """Validate all retained keys before writing an account reference or binding."""

    if type(act_no) is not str or not act_no.strip():
        raise AccountIdentityError("account_unverified")
    if type(keys) is not dict:
        raise AccountIdentityError("key_mismatch")
    for attempt in range(2):
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SELECT pg_advisory_xact_lock_shared(711)"))
                rows = (
                    (
                        await conn.execute(
                            text(
                                "SELECT key_version, key_id, key_check FROM review.nhplug_mock_key_version ORDER BY key_version"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    raise AccountIdentityError("key_registry_empty")
                bindings: dict[int, str] = {}
                for row in rows:
                    version = row["key_version"]
                    material = keys.get(version)
                    if material is None:
                        raise AccountIdentityError("key_version_unavailable")
                    if (
                        type(material) is not KeyMaterial
                        or material.version != version
                        or material.key_id != row["key_id"]
                    ):
                        raise AccountIdentityError("key_mismatch")
                    if not hmac.compare_digest(material.check(), row["key_check"]):
                        raise AccountIdentityError("key_mismatch")
                    bindings[version] = material.binding(act_no)

                refs: set[UUID] = set()
                existing: set[int] = set()
                for version, binding in bindings.items():
                    row = (
                        await conn.execute(
                            text(
                                "SELECT account_ref FROM review.nhplug_mock_account_binding "
                                "WHERE key_version=:v AND binding=:b"
                            ),
                            {"v": version, "b": binding},
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        refs.add(UUID(str(row)))
                        existing.add(version)
                if len(refs) > 1:
                    raise AccountIdentityError("account_binding_conflict")
                if refs:
                    account_ref = next(iter(refs))
                elif create:
                    account_ref = uuid4()
                    await conn.execute(
                        text(
                            "INSERT INTO review.nhplug_mock_account_ref(account_ref) VALUES (:ref)"
                        ),
                        {"ref": account_ref},
                    )
                else:
                    raise AccountIdentityError("account_binding_missing")
                if create:
                    for version, binding in bindings.items():
                        if version not in existing:
                            await conn.execute(
                                text(
                                    "INSERT INTO review.nhplug_mock_account_binding "
                                    "(key_version,binding,account_ref) VALUES (:v,:b,:ref)"
                                ),
                                {"v": version, "b": binding, "ref": account_ref},
                            )
                return account_ref
        except IntegrityError:
            if attempt == 1:
                raise AccountIdentityError("account_binding_conflict") from None
    raise AssertionError("unreachable")
