import struct

HEADER_LEN = 16


class Proto:
    def __init__(self):
        self.packet_len = 0
        self.header_len = HEADER_LEN
        self.ver = 0
        self.op = 0
        self.seq = 0
        self.body = ""
        self.max_body = 65536

    def pack(self) -> bytes:
        self.packet_len = len(self.body) + self.header_len
        buf = struct.pack(">i", self.packet_len)
        buf += struct.pack(">h", self.header_len)
        buf += struct.pack(">h", self.ver)
        buf += struct.pack(">i", self.op)
        buf += struct.pack(">i", self.seq)
        buf += self.body.encode()
        return buf

    def unpack(self, buf: bytes) -> bool:
        if len(buf) < self.header_len:
            return False
        self.packet_len = struct.unpack(">i", buf[0:4])[0]
        self.header_len = struct.unpack(">h", buf[4:6])[0]
        self.ver = struct.unpack(">h", buf[6:8])[0]
        self.op = struct.unpack(">i", buf[8:12])[0]
        self.seq = struct.unpack(">i", buf[12:16])[0]
        if self.packet_len < 0 or self.packet_len > self.max_body:
            return False
        if self.header_len != HEADER_LEN:
            return False
        body_len = self.packet_len - self.header_len
        if body_len <= 0:
            return False
        self.body = buf[self.header_len : self.packet_len].decode("utf-8")
        return True
