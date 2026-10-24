"""FernetToken: only Fernet-shaped token text reaches the ciphertext columns."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import LargeBinary, insert, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import StatementError

from family_logistics.core.crypto import AddressCipher, generate_key
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema
from family_logistics.db.column_types import FernetToken
from family_logistics.domain.models import SensitiveStr

ADDRESS = "1 Example Way, Sample Town"
LATLON = "12.3456,-65.4321"
PREFIX = "gAAAAA"
# The shortest shape the type accepts: the prefix plus 94 base64 characters.
MIN_SHAPED = PREFIX + "A" * 94


@pytest.fixture
def cipher() -> AddressCipher:
    return AddressCipher(SecretStr(generate_key()))


@pytest.fixture
def token(cipher) -> str:
    return cipher.encrypt(SensitiveStr(ADDRESS))


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    yield engine
    engine.dispose()


def bind(value: object) -> bytes | None:
    return FernetToken().process_bind_param(value, sqlite.dialect())


@pytest.mark.unit
class TestBindAccepts:
    def test_none_passes_through(self):
        assert bind(None) is None

    def test_real_token_text_is_stored_as_ascii_bytes(self, token):
        assert bind(token) == token.encode("ascii")

    def test_real_token_bytes_are_stored_unchanged(self, token):
        assert bind(token.encode("ascii")) == token.encode("ascii")

    @pytest.mark.parametrize(
        "value",
        [
            MIN_SHAPED,
            MIN_SHAPED + "=",
            MIN_SHAPED + "==",
            PREFIX + "a_-Z0" * 40,
        ],
        ids=["minimum-length", "one-pad", "two-pads", "url-safe-alphabet"],
    )
    def test_shaped_values_are_accepted(self, value):
        assert bind(value) == value.encode("ascii")

    def test_empty_plaintext_token_is_the_minimum_length(self, cipher):
        # A token for empty plaintext is the shortest Fernet can produce.
        shortest = cipher.encrypt(SensitiveStr(""))
        assert len(shortest) == 100
        assert shortest.startswith(PREFIX)
        assert bind(shortest) == shortest.encode("ascii")


FIXED_MESSAGE = "protected value rejected: not a Fernet token"
REJECTED = [
    pytest.param(ADDRESS, id="plaintext-address-text"),
    pytest.param(ADDRESS.encode(), id="plaintext-address-bytes"),
    pytest.param(LATLON, id="plaintext-latlon"),
    pytest.param("", id="empty-text"),
    pytest.param(b"", id="empty-bytes"),
    pytest.param("hAAAAA" + "A" * 94, id="wrong-prefix-text"),
    pytest.param(b"gAAAAB" + b"A" * 94, id="wrong-prefix-bytes"),
    pytest.param("A" * 100, id="no-prefix"),
    pytest.param(MIN_SHAPED[:-1], id="one-character-too-short"),
    pytest.param(PREFIX, id="prefix-only"),
    pytest.param(PREFIX + "A" * 93 + " ", id="space-in-token"),
    pytest.param(MIN_SHAPED + "\n", id="trailing-newline"),
    pytest.param(PREFIX + "A" * 90 + "+/Ab", id="standard-base64-alphabet"),
    pytest.param(MIN_SHAPED + "===", id="too-much-padding"),
    pytest.param(PREFIX + "A" * 40 + "=" + "A" * 53, id="padding-in-the-middle"),
    pytest.param(PREFIX + "A" * 93 + "é", id="non-ascii-text"),
    pytest.param(PREFIX.encode() + b"A" * 93 + b"\xff", id="non-ascii-bytes"),
    pytest.param(12345678, id="python-int"),
    pytest.param(1.5, id="python-float"),
    pytest.param(True, id="python-bool"),
    pytest.param(["gAAAAA"], id="python-list"),
    pytest.param(bytearray(MIN_SHAPED.encode()), id="python-bytearray"),
    pytest.param(SensitiveStr(ADDRESS), id="sensitive-str"),
]


@pytest.mark.unit
class TestBindRejects:
    @pytest.mark.parametrize("value", REJECTED)
    def test_value_is_rejected_with_value_error(self, value):
        with pytest.raises(ValueError, match="protected value rejected"):
            bind(value)

    @pytest.mark.parametrize("value", REJECTED)
    def test_message_never_contains_the_value(self, value):
        with pytest.raises(ValueError, match="protected value rejected") as caught:
            bind(value)
        # One fixed message is the only one ever raised, so no value is echoed.
        assert str(caught.value) == FIXED_MESSAGE

    def test_rejection_carries_no_exception_chain(self):
        with pytest.raises(ValueError, match="protected value rejected") as caught:
            bind(b"\xff" * 100)
        # A chained UnicodeDecodeError would quote a byte of the value.
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


@pytest.mark.unit
class TestResult:
    def test_stored_bytes_return_as_text(self, token):
        result = FernetToken().process_result_value(
            token.encode("ascii"), sqlite.dialect()
        )
        assert result == token
        assert isinstance(result, str)

    def test_none_passes_through(self):
        assert FernetToken().process_result_value(None, sqlite.dialect()) is None

    def test_round_trip_through_the_processors_decrypts(self, cipher, token):
        column_type = FernetToken()
        stored = column_type.process_bind_param(token, sqlite.dialect())
        loaded = column_type.process_result_value(stored, sqlite.dialect())
        assert loaded == token
        assert cipher.decrypt(loaded).reveal() == ADDRESS

    def test_ddl_type_is_a_blob_and_the_type_is_cacheable(self):
        column_type = FernetToken()
        assert column_type.cache_ok is True
        assert isinstance(column_type.impl_instance, LargeBinary)
        dialect = sqlite.dialect()
        assert column_type.compile(dialect=dialect) == LargeBinary().compile(
            dialect=dialect
        )


@pytest.mark.unit
class TestOnRealSchema:
    def test_both_columns_use_the_type(self):
        table = schema.location
        assert isinstance(table.c.address_ciphertext.type, FernetToken)
        assert isinstance(table.c.latlon_ciphertext.type, FernetToken)

    def test_insert_and_select_return_the_same_text(self, engine, cipher):
        address = cipher.encrypt(SensitiveStr(ADDRESS))
        latlon = cipher.encrypt(SensitiveStr(LATLON))
        table = schema.location
        with engine.begin() as connection:
            connection.execute(
                insert(table).values(
                    location_id="loc-1",
                    name="Example Hall",
                    address_ciphertext=address,
                    latlon_ciphertext=latlon,
                )
            )
        with engine.connect() as connection:
            row = connection.execute(
                select(table.c.address_ciphertext, table.c.latlon_ciphertext)
            ).one()
            raw = connection.exec_driver_sql(
                "SELECT typeof(address_ciphertext), "
                "hex(substr(address_ciphertext, 1, 6)) FROM location"
            ).one()
        assert row.address_ciphertext == address
        assert row.latlon_ciphertext == latlon
        assert isinstance(row.address_ciphertext, str)
        assert cipher.decrypt(row.address_ciphertext).reveal() == ADDRESS
        assert cipher.decrypt(row.latlon_ciphertext).reveal() == LATLON
        assert tuple(raw) == ("blob", "674141414141")

    def test_null_columns_round_trip_as_none(self, engine):
        table = schema.location
        with engine.begin() as connection:
            connection.execute(
                insert(table).values(location_id="loc-1", name="Example Hall")
            )
        with engine.connect() as connection:
            row = connection.execute(
                select(table.c.address_ciphertext, table.c.latlon_ciphertext)
            ).one()
        assert (row.address_ciphertext, row.latlon_ciphertext) == (None, None)

    @pytest.mark.parametrize("column", ["address_ciphertext", "latlon_ciphertext"])
    def test_plaintext_is_rejected_before_it_is_stored(self, engine, column):
        table = schema.location
        with (
            pytest.raises(StatementError, match="protected value rejected") as caught,
            engine.begin() as connection,
        ):
            connection.execute(
                insert(table).values(
                    location_id="loc-1", name="Example Hall", **{column: ADDRESS}
                )
            )
        assert isinstance(caught.value.orig, ValueError)
        with engine.connect() as connection:
            assert connection.scalar(select(table.c.location_id)) is None
