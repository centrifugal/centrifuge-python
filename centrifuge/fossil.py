z_value = [
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    -1,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    25,
    26,
    27,
    28,
    29,
    30,
    31,
    32,
    33,
    34,
    35,
    -1,
    -1,
    -1,
    -1,
    36,
    -1,
    37,
    38,
    39,
    40,
    41,
    42,
    43,
    44,
    45,
    46,
    47,
    48,
    49,
    50,
    51,
    52,
    53,
    54,
    55,
    56,
    57,
    58,
    59,
    60,
    61,
    62,
    -1,
    -1,
    -1,
    63,
    -1,
]


class Reader:
    def __init__(self, array):
        self.a = array  # source array
        self.pos = 0  # current position in array

    def have_bytes(self):
        return self.pos < len(self.a)

    def get_byte(self):
        if self.pos >= len(self.a):
            raise IndexError("out of bounds")
        b = self.a[self.pos]
        self.pos += 1
        return b

    def get_char(self):
        return chr(self.get_byte())

    # Read base64-encoded unsigned integer.
    def get_int(self):
        v = 0
        while self.have_bytes():
            byte = self.get_byte()
            c = z_value[byte & 0x7F]
            if c < 0:
                break
            v = (v << 6) + c
        self.pos -= 1
        return v & 0xFFFFFFFF  # Ensure unsigned 32-bit integer


# Writer writes an array.
class Writer:
    def __init__(self):
        self.a = []  # Internal array to store data

    def to_byte_array(self, source_type):
        if isinstance(source_type, list):
            return self.a  # Return as list
        return bytes(self.a)  # Return as bytes

    # Copy from array at start to end.
    def put_array(self, a, start, end):
        # Copy elements from array 'a' from 'start' to 'end' into self.a
        self.a.extend(a[start:end])


# Return a 32-bit checksum of the array.
def checksum(arr):
    sum0 = sum1 = sum2 = sum3 = 0
    z = 0
    _n = len(arr)
    # Unrolling the loop for performance
    while _n >= 16:
        sum0 = (sum0 + arr[z + 0]) & 0xFFFFFFFF
        sum1 = (sum1 + arr[z + 1]) & 0xFFFFFFFF
        sum2 = (sum2 + arr[z + 2]) & 0xFFFFFFFF
        sum3 = (sum3 + arr[z + 3]) & 0xFFFFFFFF

        sum0 = (sum0 + arr[z + 4]) & 0xFFFFFFFF
        sum1 = (sum1 + arr[z + 5]) & 0xFFFFFFFF
        sum2 = (sum2 + arr[z + 6]) & 0xFFFFFFFF
        sum3 = (sum3 + arr[z + 7]) & 0xFFFFFFFF

        sum0 = (sum0 + arr[z + 8]) & 0xFFFFFFFF
        sum1 = (sum1 + arr[z + 9]) & 0xFFFFFFFF
        sum2 = (sum2 + arr[z + 10]) & 0xFFFFFFFF
        sum3 = (sum3 + arr[z + 11]) & 0xFFFFFFFF

        sum0 = (sum0 + arr[z + 12]) & 0xFFFFFFFF
        sum1 = (sum1 + arr[z + 13]) & 0xFFFFFFFF
        sum2 = (sum2 + arr[z + 14]) & 0xFFFFFFFF
        sum3 = (sum3 + arr[z + 15]) & 0xFFFFFFFF

        z += 16
        _n -= 16

    while _n >= 4:
        sum0 = (sum0 + arr[z + 0]) & 0xFFFFFFFF
        sum1 = (sum1 + arr[z + 1]) & 0xFFFFFFFF
        sum2 = (sum2 + arr[z + 2]) & 0xFFFFFFFF
        sum3 = (sum3 + arr[z + 3]) & 0xFFFFFFFF
        z += 4
        _n -= 4

    sum3 = (sum3 + (sum2 << 8) + (sum1 << 16) + (sum0 << 24)) & 0xFFFFFFFF

    # Handle remaining bytes
    if _n == 3:
        sum3 = (sum3 + (arr[z + 2] << 8)) & 0xFFFFFFFF
        sum3 = (sum3 + (arr[z + 1] << 16)) & 0xFFFFFFFF
        sum3 = (sum3 + (arr[z + 0] << 24)) & 0xFFFFFFFF
    elif _n == 2:
        sum3 = (sum3 + (arr[z + 1] << 16)) & 0xFFFFFFFF
        sum3 = (sum3 + (arr[z + 0] << 24)) & 0xFFFFFFFF
    elif _n == 1:
        sum3 = (sum3 + (arr[z + 0] << 24)) & 0xFFFFFFFF

    return sum3 & 0xFFFFFFFF  # Ensure unsigned 32-bit integer


# Apply a delta byte array to a source byte array, returning the target byte array.
def apply_delta(source, delta):  # noqa: PLR0912
    total = 0
    z_delta = Reader(delta)
    len_src = len(source)
    len_delta = len(delta)

    limit = z_delta.get_int()
    c = z_delta.get_char()
    if c != "\n":
        raise ValueError("size integer not terminated by '\\n'")

    z_out = Writer()
    while z_delta.have_bytes():
        cnt = z_delta.get_int()
        operator = z_delta.get_char()

        if operator == "@":
            ofst = z_delta.get_int()
            if z_delta.have_bytes() and z_delta.get_char() != ",":
                raise ValueError("copy command not terminated by ','")
            total += cnt
            if total > limit:
                raise ValueError("copy exceeds output file size")
            if ofst + cnt > len_src:
                raise ValueError("copy extends past end of input")
            z_out.put_array(source, ofst, ofst + cnt)

        elif operator == ":":
            total += cnt
            if total > limit:
                raise ValueError("insert command gives an output larger than predicted")
            if cnt > len_delta - z_delta.pos:
                raise ValueError("insert count exceeds size of delta")
            z_out.put_array(z_delta.a, z_delta.pos, z_delta.pos + cnt)
            z_delta.pos += cnt

        elif operator == ";":
            out = z_out.to_byte_array(source)
            if cnt != checksum(out):
                raise ValueError("bad checksum")
            if total != limit:
                raise ValueError("generated size does not match predicted size")
            return out

        else:
            raise ValueError("unknown delta operator")

    raise ValueError("unterminated delta")
