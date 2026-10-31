"""Regression tests for Modbus reply alignment and PV counter validation."""
import asyncio
import importlib.util
import itertools
from pathlib import Path
import struct
import os
import unittest

os.environ.setdefault('SUN2000_HOST', '127.0.0.1')

spec = importlib.util.spec_from_file_location('modbus_cache', Path(__file__).parents[1] / 'modbus_cache_server.py')
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)

class Writer:
    def __init__(self): self.requests = []
    def write(self, data): self.requests.append(data)
    async def drain(self): pass

class ModbusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cache.init_cache()
        cache.transaction_ids = itertools.count(0)

    def response(self, tx=1, unit=1, fc=3, words=(0, 123), count=None):
        payload = struct.pack('>' + 'H' * len(words), *words)
        pdu = bytes([fc, len(payload) if count is None else count]) + payload
        return struct.pack('>HHHB', tx, 0, len(pdu) + 1, unit) + pdu

    async def test_fragmented_header_and_body(self):
        reader = asyncio.StreamReader(); writer = Writer()
        task = asyncio.create_task(cache.read_batch(reader, writer, 32114, 2))
        data = self.response()
        for byte in data:
            reader.feed_data(bytes([byte])); await asyncio.sleep(0)
        self.assertEqual(await task, (0, 123))

    async def test_late_reply_cannot_become_next_register(self):
        reader = asyncio.StreamReader(); reader.feed_data(self.response(tx=0, words=(46, 12000)) + self.response(words=(0,123)))
        self.assertEqual(await cache.read_batch(reader, Writer(), 32114, 2), (0,123))

    async def test_wrong_unit(self):
        reader = asyncio.StreamReader(); reader.feed_data(self.response(unit=2)); reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await cache.read_batch(reader, Writer(), 32114, 2)

    async def test_wrong_byte_count(self):
        reader = asyncio.StreamReader(); reader.feed_data(self.response(count=2))
        with self.assertRaises(cache.InvalidResponse):
            await cache.read_batch(reader, Writer(), 32114, 2)

    async def test_complete_exception_is_consumed(self):
        reader = asyncio.StreamReader(); reader.feed_data(struct.pack('>HHHBBB', 1, 0, 3, 1, 0x83, 2) + self.response(tx=2))
        self.assertIsNone(await cache.read_batch(reader, Writer(), 32114, 2))
        self.assertEqual(await cache.read_batch(reader, Writer(), 32114, 2), (0, 123))

    async def test_short_frame_fails(self):
        reader = asyncio.StreamReader(); reader.feed_data(self.response()[:-1]); reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await cache.read_batch(reader, Writer(), 32114, 2)

    async def test_missing_cache_returns_exception_not_zero(self):
        server = await asyncio.start_server(cache.handle_client, '127.0.0.1', 0)
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            writer.write(struct.pack('>HHHBBHH', 10, 0, 6, 1, 3, 32106, 2)); await writer.drain()
            self.assertEqual(await reader.readexactly(9), struct.pack('>HHHBBB', 10, 0, 3, 1, 0x83, 11))
            writer.close(); await writer.wait_closed()
        finally:
            server.close(); await server.wait_closed()

class YieldTests(unittest.TestCase):
    def setUp(self): cache.init_cache()
    def words(self, value):
        raw = round(value * 100); return (raw >> 16, raw & 65535)
    def test_unsigned_invalid_value_rejected(self):
        self.assertFalse(cache.valid_yield(32114, (65535,65532), 1793340000))
    def test_total_cannot_be_daily(self):
        self.assertFalse(cache.valid_yield(32114, self.words(30400), 1793340000))
    def test_normal_increase_and_backwards_rejection(self):
        t=1793340000
        self.assertTrue(cache.valid_yield(32106,self.words(30400),t))
        self.assertTrue(cache.valid_yield(32106,self.words(30400.1),t+60))
        self.assertFalse(cache.valid_yield(32106,self.words(30400),t+120))
        self.assertFalse(cache.valid_yield(32106,self.words(30450),t+120))
    def test_daily_reset_only_on_new_day(self):
        from datetime import datetime
        t=datetime(2026,10,29,23,59).timestamp()
        self.assertTrue(cache.valid_yield(32114,self.words(20),t))
        self.assertFalse(cache.valid_yield(32114,self.words(0),t+10))
        self.assertTrue(cache.valid_yield(32114,self.words(0),t+120))

    def test_single_wrong_read_stays_rejected(self):
        t=1_800_000_000
        self.assertTrue(cache.valid_yield(32106,self.words(30000),t))
        self.assertFalse(cache.valid_yield(32106,self.words(18.26),t+13))
        self.assertTrue(cache.valid_yield(32106,self.words(30000),t+26))
    def test_wrong_first_read_does_not_block_the_counter(self):
        t=1_800_000_000
        self.assertTrue(cache.valid_yield(32106,self.words(18.26),t))
        self.assertFalse(cache.valid_yield(32106,self.words(30000),t+13))
        self.assertFalse(cache.valid_yield(32106,self.words(30000),t+26))
        self.assertTrue(cache.valid_yield(32106,self.words(30000),t+39))
    def test_daily_reset_just_after_midnight(self):
        from datetime import datetime
        t=datetime(2026,10,31,0,0,5).timestamp()
        self.assertTrue(cache.valid_yield(32114,self.words(15.4),t))
        self.assertFalse(cache.valid_yield(32114,self.words(0),t+40))
        self.assertFalse(cache.valid_yield(32114,self.words(0),t+53))
        self.assertTrue(cache.valid_yield(32114,self.words(0),t+66))

class TransactionIdTests(unittest.TestCase):
    def test_id_zero_is_never_used(self):
        cache.transaction_ids=itertools.count(65534)
        self.assertEqual([cache.next_transaction_id() for _ in range(3)],[65535,1,2])

if __name__ == '__main__': unittest.main()
