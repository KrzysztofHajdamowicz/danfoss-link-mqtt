"""Bounded Protobuf codec based on literal descriptors recovered from the APK."""

import json
import struct
from pathlib import Path

SCHEMAS = json.loads(Path(__file__).with_name("schemas.json").read_text())
MAX_BYTES = 65535


class WireError(Exception):
    pass


def varint(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise WireError("Invalid unsigned value")
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def read_varint(data, offset):
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise WireError("Truncated unsigned value")
        value = data[offset]
        offset += 1
        if shift == 63 and value > 1:
            raise WireError("Unsigned overflow")
        result |= (value & 127) << shift
        if value < 128:
            return result, offset
    raise WireError("Unsigned overflow")


def _schema(module, message):
    try:
        return SCHEMAS[module]["messages"][message]
    except KeyError:
        raise WireError("Unknown message schema") from None


def encode(module, message, values):
    schema = _schema(module, message)
    if set(values) - set(schema):
        raise WireError("Unknown field")
    out = bytearray()
    for name, desc in sorted(schema.items(), key=lambda item: item[1]["number"]):
        if name not in values:
            if desc["label"] == 2:
                raise WireError("Required field absent")
            continue
        seq = values[name] if desc["label"] == 3 else [values[name]]
        if not isinstance(seq, (list, tuple)):
            raise WireError("Invalid repeated field")
        for value in seq:
            kind = desc["type"]
            if kind in (4, 13, 14, 8):
                if kind == 8:
                    if type(value) is not bool:
                        raise WireError("Invalid boolean")
                    value = int(value)
                if kind == 13 and (type(value) is not int or not 0 <= value < 2**32):
                    raise WireError("Unsigned 32-bit overflow")
                payload = varint(value)
                wire = 0
            elif kind == 7:
                if type(value) is not int or not 0 <= value < 2**32:
                    raise WireError("Invalid fixed32")
                payload = struct.pack("<I", value)
                wire = 5
            elif kind in (9, 12, 11):
                if kind == 9:
                    if not isinstance(value, str):
                        raise WireError("Invalid string")
                    value = value.encode("utf-8")
                elif kind == 11:
                    value = encode(module, desc["message"], value)
                if not isinstance(value, bytes):
                    raise WireError("Invalid bytes")
                payload = varint(len(value)) + value
                wire = 2
            else:
                raise WireError("Unsupported field type")
            out.extend(varint(desc["number"] << 3 | wire))
            out.extend(payload)
            if len(out) > MAX_BYTES:
                raise WireError("Message too large")
    return bytes(out)


def decode(module, message, data):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise WireError("Invalid message size")
    schema = _schema(module, message)
    numbers = {desc["number"]: (name, desc) for name, desc in schema.items()}
    out = {}
    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise WireError("Invalid field tag")
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if offset + size > len(data):
                raise WireError("Truncated fixed field")
            value = int.from_bytes(data[offset : offset + size], "little")
            offset += size
        elif wire == 2:
            size, offset = read_varint(data, offset)
            if offset + size > len(data):
                raise WireError("Truncated field")
            value = data[offset : offset + size]
            offset += size
        else:
            raise WireError("Unsupported wire type")
        if number not in numbers:
            continue
        name, desc = numbers[number]
        kind = desc["type"]
        expected = 0 if kind in (4, 13, 14, 8) else 5 if kind == 7 else 2
        if wire != expected:
            raise WireError("Field wire type mismatch")
        if kind == 13 and value >= 2**32:
            raise WireError("Unsigned 32-bit overflow")
        if kind == 8:
            if value not in (0, 1):
                raise WireError("Invalid boolean")
            value = bool(value)
        elif kind == 9:
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                raise WireError("Invalid UTF-8") from None
        elif kind == 11:
            value = decode(module, desc["message"], value)
        if desc["label"] == 3:
            out.setdefault(name, []).append(value)
        else:
            if name in out:
                raise WireError("Duplicate scalar field")
            out[name] = value
    if any(desc["label"] == 2 and name not in out for name, desc in schema.items()):
        raise WireError("Required field absent")
    return out


def packet(module, message, values, code):
    if type(code) is not int or not 0 <= code <= 255:
        raise WireError("Invalid message code")
    return bytes([code]) + encode(module, message, values)
