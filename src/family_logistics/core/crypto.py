"""Application-level encryption for protected addresses (privacy spec section 5).

External household addresses and home coordinates are stored encrypted with a
Fernet key supplied at runtime (``FL_DATA_KEY``, from the deployment's secret
store). Plaintext exists only inside ``SensitiveStr`` values,
which never render in ``repr()``, ``str()`` or logs.

#CRITICAL: Security: losing the key loses only the protected fields; they are
re-entered from the registry UI. Never log tokens or plaintext.
"""

from __future__ import annotations

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
        self._fernet = Fernet(key.get_secret_value().encode("ascii"))

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
