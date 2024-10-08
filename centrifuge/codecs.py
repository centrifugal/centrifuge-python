import json
from typing import Union, Iterable, AsyncIterable, TYPE_CHECKING

from google.protobuf.json_format import MessageToDict, ParseDict
from websockets.typing import Data

import centrifuge.protocol.client_pb2 as protocol
from centrifuge.fossil import apply_delta

if TYPE_CHECKING:
    from centrifuge import Publication


class _JsonCodec:
    """_JsonCodec is a default codec for Centrifuge library. It encodes commands using JSON."""

    @staticmethod
    def encode_commands(commands):
        return "\n".join(json.dumps(command) for command in commands)

    @staticmethod
    def decode_replies(data):
        return [json.loads(reply) for reply in data.strip().split("\n")]

    @staticmethod
    def apply_delta_if_needed(prev_data: bytes, pub: "Publication"):
        if pub.delta:
            prev_data = apply_delta(prev_data, pub.data.encode("utf-8"))
            new_data = json.loads(prev_data)
        else:
            prev_data = pub.data.encode("utf-8")
            new_data = json.loads(pub.data)
        return new_data, prev_data


def _varint_encode(number):
    """Encode an integer as a varint."""
    buffer = []
    while True:
        towrite = number & 0x7F
        number >>= 7
        if number:
            buffer.append(towrite | 0x80)
        else:
            buffer.append(towrite)
            break
    return bytes(buffer)


def _varint_decode(buffer: bytes, position: int):
    """Decode a varint from buffer starting at position."""
    result = 0
    shift = 0
    while True:
        byte = buffer[position]
        position += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return result, position


class _ProtobufCodec:
    """_ProtobufCodec encodes commands using Protobuf protocol."""

    @staticmethod
    def encode_commands(commands: Union[Data, Iterable[Data], AsyncIterable[Data]]):
        serialized_commands = []
        for command in commands:
            # noinspection PyUnresolvedReferences
            serialized = ParseDict(command, protocol.Command()).SerializeToString()
            serialized_commands.append(_varint_encode(len(serialized)) + serialized)
        return b"".join(serialized_commands)

    @staticmethod
    def decode_replies(data: bytes):
        replies = []
        position = 0
        while position < len(data):
            message_length, position = _varint_decode(data, position)
            message_end = position + message_length
            message_bytes = data[position:message_end]
            position = message_end
            # noinspection PyUnresolvedReferences
            reply = protocol.Reply()
            reply.ParseFromString(message_bytes)
            replies.append(MessageToDict(reply, preserving_proto_field_name=True))
        return replies

    @staticmethod
    def apply_delta_if_needed(prev_data: bytes, pub: "Publication"):
        if pub.delta:
            prev_data = apply_delta(prev_data, pub.data)
            new_data = prev_data
        else:
            prev_data = pub.data
            new_data = pub.data
        return new_data, prev_data
