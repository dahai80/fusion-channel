from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

KEY_BYTES = 32
NONCE_BYTES = 12
MAGIC = b"FCH1"
TEXT_PREFIX = "enc:v1:"


class CredentialError(Exception):
    pass


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return value[:2] + "***"
    return f"{value[:4]}***{value[-2:]}"


class CredentialProvider:
    # Interface: get/put/mask. Library never persists credentials itself;
    # the default file-backed implementation uses AES-256-GCM at rest.

    def get(self, name: str, default: str = "") -> str:
        raise NotImplementedError

    def put(self, name: str, value: str) -> None:
        raise NotImplementedError

    def describe(self, names: list[str]) -> dict[str, str]:
        return {name: self.mask_value(name) for name in names}

    def mask_value(self, name: str) -> str:
        return mask(self.get(name))


class EnvCredentialProvider(CredentialProvider):
    def __init__(self, prefix: str = "FUSION_CHANNEL_"):
        self.prefix = prefix

    def _key(self, name: str) -> str:
        return self.prefix + name.upper()

    def get(self, name: str, default: str = "") -> str:
        return os.environ.get(self._key(name), default)

    def put(self, name: str, value: str) -> None:
        raise CredentialError("EnvCredentialProvider is read-only")


class MemoryCredentialProvider(CredentialProvider):
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    def get(self, name: str, default: str = "") -> str:
        return self.values.get(name, default)

    def put(self, name: str, value: str) -> None:
        self.values[name] = value


class AESGCMCredentialProvider(CredentialProvider):
    # Ported from ai-family secret_store (AES-256-GCM, 0600 key file, atomic writes).

    def __init__(self, data_dir: str | Path, key_file: str | Path | None = None):
        self.data_dir = Path(data_dir)
        self.key_file = Path(key_file) if key_file else self.data_dir / ".secret_key"
        self._key: bytes | None = None
        self.secrets_dir = self.data_dir / "secrets"

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        path = self.key_file
        if path.exists():
            raw = path.read_bytes()
            if len(raw) != KEY_BYTES:
                raise CredentialError(f"secret key corrupt path={path} bytes={len(raw)}")
            self._key = raw
            return raw
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = os.urandom(KEY_BYTES)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.chmod(path, 0o600)
        self._key = raw
        logger.info("channel secret key generated path=%s", path)
        return raw

    def _encrypt(self, raw: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self._load_key()).encrypt(nonce, raw, None)
        return MAGIC + nonce + ciphertext

    def _decrypt(self, blob: bytes) -> bytes:
        if not blob.startswith(MAGIC) or len(blob) <= len(MAGIC) + NONCE_BYTES:
            raise CredentialError("encrypted payload has invalid header")
        nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
        ciphertext = blob[len(MAGIC) + NONCE_BYTES :]
        return AESGCM(self._load_key()).decrypt(nonce, ciphertext, None)

    def encrypt_text(self, text: str) -> str:
        return TEXT_PREFIX + base64.b64encode(self._encrypt(text.encode("utf-8"))).decode("ascii")

    def decrypt_text(self, token: str) -> str:
        if not token.startswith(TEXT_PREFIX):
            raise CredentialError("not an encrypted text token")
        blob = base64.b64decode(token[len(TEXT_PREFIX) :])
        return self._decrypt(blob).decode("utf-8")

    def _path(self, name: str) -> Path:
        return self.secrets_dir / f"{name}.enc"

    def put(self, name: str, value: str) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        blob = self._encrypt(value.encode("utf-8"))
        tmp = self._path(name).with_suffix(".enc.tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        tmp.replace(self._path(name))
        logger.info("channel secret saved name=%s", name)

    def get(self, name: str, default: str = "") -> str:
        path = self._path(name)
        if not path.exists():
            return default
        try:
            return self._decrypt(path.read_bytes()).decode("utf-8")
        except Exception as exc:
            logger.error("channel secret load failed name=%s err=%s", name, exc)
            raise

    def put_json(self, name: str, data: dict) -> None:
        self.put(name, json.dumps(data, ensure_ascii=False, sort_keys=True))

    def get_json(self, name: str) -> dict | None:
        raw = self.get(name, "")
        if not raw:
            return None
        return json.loads(raw)


class ChainedCredentialProvider(CredentialProvider):
    # First provider that has the value wins; writes go to the first writable one.
    def __init__(self, *providers: CredentialProvider):
        if not providers:
            raise CredentialError("ChainedCredentialProvider needs at least one provider")
        self.providers = list(providers)

    def get(self, name: str, default: str = "") -> str:
        for provider in self.providers:
            value = provider.get(name, "")
            if value:
                return value
        return default

    def put(self, name: str, value: str) -> None:
        for provider in self.providers:
            try:
                provider.put(name, value)
                return
            except CredentialError:
                continue
        raise CredentialError("no writable credential provider in chain")

    def mask_value(self, name: str) -> str:
        for provider in self.providers:
            value = provider.get(name, "")
            if value:
                return mask(value)
        return ""
