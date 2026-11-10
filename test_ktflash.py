"""Tests for ktflash. Run: python3 -m unittest test_ktflash -v

Set KT_TEST_IMAGE=/path/to/JadeAudio_JA11_V2.2.bin to also test against the real image.
"""
import os
import struct
import unittest

import ktflash as kt


def synth_image(size=5000, chip=b"KT02H20B"):
    d = bytearray(os.urandom(size))
    d[:16] = kt.IMAGE_MAGIC
    d[0x10:0x18] = chip
    struct.pack_into("<I", d, 0x1C, size)
    return bytes(d)


class MockBootloader:
    """Emulates the decoded protocol and reassembles the flash from packets."""

    def __init__(self, chip=b"KT02H20B", drop_ack_at=None):
        self.chip, self.rx, self.flash = chip, b"", {}
        self.state, self.handshaken, self.sent = "hs", False, []
        self.drop_ack_at, self.npkt = drop_ack_at, 0
        self.reset_seen = False

    # link API used by Bootloader
    def drain(self):
        self.rx = b""

    def read_some(self):
        out, self.rx = self.rx, b""
        return out

    def close(self):
        pass

    def send(self, data: bytes):
        self.sent.append(data)
        if data == kt.CMD_KTM:
            if not self.handshaken:
                self.handshaken = True
                self.rx += b"\x78"
        elif data == kt.CMD_CHP:
            self.rx += b"\xb2" + b"\x85\x40\x12"[:3] + self.chip + b"\x76"
        elif data in (kt.CMD_ERASE, kt.CMD_PWO, kt.CMD_KSTA, kt.CMD_STP):
            self.rx += b"\x78"
        elif data == kt.CMD_RESET:
            self.reset_seen = True
            self.rx += b"\x78"
        elif data[0] == 0x69:
            body, crc = data[:-4], struct.unpack("<I", data[-4:])[0]
            assert crc == kt.crc32_kt(body), "bad CRC"
            n = body[1] | ((body[2] & 0x1F) << 8)
            addr = body[3] | body[4] << 8 | body[5] << 16
            payload = body[6:]
            assert len(payload) == n, "length field mismatch"
            for i, b in enumerate(payload):
                self.flash[addr + i] = b
            self.npkt += 1
            if self.drop_ack_at is None or self.npkt != self.drop_ack_at:
                self.rx += b"\x78\xa5"
        else:
            raise AssertionError("unexpected command %s" % data.hex())


class TestCrc(unittest.TestCase):
    def test_table_and_variant(self):
        self.assertEqual(kt._TABLE[1], 0x77073096)
        # init 0 / no final xor => differs from zlib crc32
        import zlib
        self.assertNotEqual(kt.crc32_kt(b"123456789"), zlib.crc32(b"123456789"))
        # but equals zlib crc32 transformed: zlib uses init/final 0xFFFFFFFF
        def bitwise(data):
            c = 0
            for b in data:
                c ^= b
                for _ in range(8):
                    c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
            return c
        self.assertEqual(kt.crc32_kt(b"\x01\x02hello"), bitwise(b"\x01\x02hello"))


class TestImage(unittest.TestCase):
    def test_parse_ok(self):
        img = kt.Image.parse(synth_image())
        self.assertEqual(img.chip_id, b"KT02H20B")

    def test_rejects_bad_magic_and_size(self):
        d = bytearray(synth_image()); d[0] = 0
        with self.assertRaises(kt.KtError):
            kt.Image.parse(bytes(d))
        d = bytearray(synth_image()); struct.pack_into("<I", d, 0x1C, 1234)
        with self.assertRaises(kt.KtError):
            kt.Image.parse(bytes(d))


class TestPlanner(unittest.TestCase):
    def test_headers(self):
        img = kt.Image.parse(synth_image(70000))
        plan = kt.plan_packets(img)
        self.assertEqual(plan[0].data[:6].hex(), "69f0231000" + "00")   # 0x3f0 bytes, bank 1, addr 0x10
        self.assertEqual(plan[1].data[2] >> 5, 0b111)
        self.assertEqual(plan[32].data[2] >> 5, 1)                      # region start (block 32)
        self.assertEqual(plan[-1].data[:6].hex(), "6910e0000000")       # final header packet
        self.assertTrue(plan[-1].final)

    def test_roundtrip_reassembles_image(self):
        for size in (1025, 5000, 70000, 1024 * 33):
            raw = synth_image(size)
            img = kt.Image.parse(raw)
            mock = MockBootloader()
            kt.Bootloader(mock, log=lambda s: None).flash(kt.plan_packets(img))
            flash = bytes(mock.flash[i] for i in range(size))
            self.assertEqual(flash, raw, "size %d" % size)

    def test_real_image_if_available(self):
        path = os.environ.get("KT_TEST_IMAGE")
        if not path:
            self.skipTest("KT_TEST_IMAGE not set")
        img = kt.Image.load(path)
        plan = kt.plan_packets(img)
        self.assertEqual(img.chip_id, b"KT02H20B")
        self.assertEqual(len(plan), 67)          # 66 data + 1 final for the 67312-byte JA11 V2.2 image
        mock = MockBootloader()
        kt.Bootloader(mock, log=lambda s: None).flash(plan)
        self.assertEqual(bytes(mock.flash[i] for i in range(img.size)), img.data)


class TestSequence(unittest.TestCase):
    def test_order_and_final_last(self):
        img = kt.Image.parse(synth_image())
        mock = MockBootloader()
        bl = kt.Bootloader(mock, log=lambda s: None)
        bl.handshake(); self.assertEqual(bl.chip_id(), b"KT02H20B")
        bl.flash(kt.plan_packets(img))
        order = [s[:4].hex() for s in mock.sent if s[0] != 0x69]
        self.assertEqual(order, [kt.CMD_KTM.hex(), kt.CMD_CHP.hex(), kt.CMD_ERASE.hex()[:8],
                                 kt.CMD_PWO.hex(), kt.CMD_KSTA.hex(), kt.CMD_STP.hex(), kt.CMD_RESET.hex()])
        pk = [s for s in mock.sent if s[0] == 0x69]
        self.assertEqual(pk[-1][:6].hex(), "6910e0000000")      # header written last

    def test_missing_block_ack_aborts_with_position(self):
        img = kt.Image.parse(synth_image(5000))
        mock = MockBootloader(drop_ack_at=3)
        import ktflash
        orig = ktflash.wait_for
        ktflash.wait_for = lambda link, w, t: orig(link, w, 0.05)   # keep the test fast
        try:
            with self.assertRaises(kt.KtError) as cm:
                kt.Bootloader(mock, log=lambda s: None).flash(kt.plan_packets(img))
        finally:
            ktflash.wait_for = orig
        self.assertIn("packet 3/", str(cm.exception))


class TestSafety(unittest.TestCase):
    def test_flash_refuses_without_ack_flags(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(synth_image())
        try:
            rc = kt.main(["flash", f.name, "--execute"])
            self.assertEqual(rc, 1)
            self.assertEqual(kt.main(["flash", f.name]), 0)      # dry run is fine
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
