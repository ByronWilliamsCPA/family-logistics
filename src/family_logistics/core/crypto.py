"""Application-level encryption for protected addresses (privacy spec section 5).

External household addresses and home coordinates are stored encrypted with a
Fernet key supplied at runtime (``FL_DATA_KEY``, from the deployment's secret
store). Plaintext exists only inside ``SensitiveStr`` values,
which never render in ``repr()``, ``str()`` or logs.

#CRITICAL: Security: losing the key loses only the protected fields; they are
re-entered from the registry UI. Never log tokens or plaintext.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

from family_logistics.core.exceptions import ConfigurationError
from family_logistics.domain.models import SensitiveStr

if TYPE_CHECKING:
    from pydantic import SecretStr


class ProtectedDataError(ValueError):
    """Raised when a protected value cannot be decrypted with the current key."""


class AddressCipher:
    """Encrypts and decrypts protected address text.

    Args:
        key: Fernet key from settings.
    """

    def __init__(self, key: SecretStr) -> None:
        raw = key.get_secret_value().encode("ascii")
        self._fernet = Fernet(raw)
        self._fingerprint_key = hashlib.sha256(b"fl-fingerprint\x1f" + raw).digest()

    def fingerprint(self, value: SensitiveStr) -> str:
        """Return a keyed digest of a sensitive value for change detection.

        Fernet tokens differ on every encryption, so stored ciphertext cannot be
        compared. The keyed HMAC lets the registry detect an address change and
        record it in the audit log without storing or hashing the plaintext
        under a public function (a plain SHA-256 of a street address could be
        brute-forced).

        Args:
            value: Plaintext wrapped in ``SensitiveStr``.

        Returns:
            Hex HMAC-SHA256 of the value under a key derived from FL_DATA_KEY.
        """
        return hmac.new(
            self._fingerprint_key, value.reveal().encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def encrypt(self, value: SensitiveStr) -> str:
        """Encrypt a sensitive value for storage.

        Args:
            value: Plaintext wrapped in ``SensitiveStr``.

        Returns:
            Fernet token text, safe to store.
        """
        return self._fernet.encrypt(value.reveal().encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> SensitiveStr:
        """Decrypt a stored token.

        Args:
            token: Fernet token text.

        Returns:
            The plaintext wrapped in ``SensitiveStr``.

        Raises:
            ProtectedDataError: If the token is invalid or the key is wrong.
        """
        try:
            plain = self._fernet.decrypt(token.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError) as exc:
            msg = "Protected value could not be decrypted with FL_DATA_KEY"
            raise ProtectedDataError(msg) from exc
        return SensitiveStr(plain.decode("utf-8"))


def require_cipher(key: SecretStr | None) -> AddressCipher:
    """Return a cipher, refusing to proceed without a data key.

    Called by any code path that stores a protected address.

    Args:
        key: ``Settings.data_key``.

    Returns:
        The cipher.

    Raises:
        ConfigurationError: If no key is configured.
    """
    if key is None:
        msg = "FL_DATA_KEY is required before storing protected addresses"
        raise ConfigurationError(msg)
    return AddressCipher(key)


def generate_key() -> str:
    """Generate a new Fernet key for ``FL_DATA_KEY``.

    Returns:
        URL-safe base64 key text.
    """
    return Fernet.generate_key().decode("ascii")
