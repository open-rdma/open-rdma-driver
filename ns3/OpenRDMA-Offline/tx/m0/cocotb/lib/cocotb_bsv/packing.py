"""Strict integer packing with Bluespec struct field ordering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, ClassVar


class PackingError(ValueError):
    pass


def _unsigned(value: Any, *, context: str) -> int:
    if isinstance(value, bytes):
        return int.from_bytes(value, byteorder="little", signed=False)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PackingError(f"{context} must be an unsigned integer") from exc


class Bits:
    _width: ClassVar[int] = 0

    def __init__(self, value: Any = 0) -> None:
        if isinstance(value, Bits):
            value = value.pack()
        integer = _unsigned(value, context=type(self).__name__)
        if integer < 0 or integer >= (1 << self.width()):
            raise PackingError(
                f"{type(self).__name__} value {integer} does not fit in {self.width()} bits"
            )
        self._value = integer

    def pack(self) -> int:
        return self._value

    @classmethod
    def unpack(cls, value: Any) -> "Bits":
        return cls(value)

    @classmethod
    def width(cls) -> int:
        return cls._width

    def __int__(self) -> int:
        return self.pack()


class Struct:
    _members_def: ClassVar[Mapping[str, type[Bits] | type["Struct"]]] = OrderedDict()

    def __init__(self, *members: Any, **named_members: Any) -> None:
        definitions = OrderedDict(self._members_def)
        if members and named_members:
            raise PackingError("struct fields must be positional or named, not both")
        if members:
            if len(members) != len(definitions):
                raise PackingError(
                    f"{type(self).__name__} expects {len(definitions)} fields, got {len(members)}"
                )
            values = dict(zip(definitions, members, strict=True))
        else:
            missing = definitions.keys() - named_members.keys()
            extra = named_members.keys() - definitions.keys()
            if missing or extra:
                raise PackingError(
                    f"invalid {type(self).__name__} fields: "
                    f"missing={sorted(missing)!r} extra={sorted(extra)!r}"
                )
            values = named_members
        object.__setattr__(self, "_members", OrderedDict())
        for name, field_type in definitions.items():
            value = values[name]
            self._members[name] = (
                value if isinstance(value, field_type) else field_type(value)
            )

    def pack(self) -> int:
        packed = 0
        for member in self._members.values():
            packed = (packed << member.width()) | member.pack()
        return packed

    @classmethod
    def unpack(cls, value: Any) -> "Struct":
        integer = _unsigned(value, context=cls.__name__)
        if integer < 0 or integer >= (1 << cls.width()):
            raise PackingError(f"{cls.__name__} value does not fit in {cls.width()} bits")
        fields = []
        for field_type in reversed(tuple(cls._members_def.values())):
            width = field_type.width()
            fields.append(field_type.unpack(integer & ((1 << width) - 1)))
            integer >>= width
        return cls(*reversed(fields))

    @classmethod
    def width(cls) -> int:
        return sum(field_type.width() for field_type in cls._members_def.values())

    def __getattr__(self, name: str) -> Any:
        try:
            return self._members[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __repr__(self) -> str:
        fields = ", ".join(f"{name}={int(value):#x}" for name, value in self._members.items())
        return f"{type(self).__name__}({fields})"
