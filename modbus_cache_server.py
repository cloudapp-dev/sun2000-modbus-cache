#!/usr/bin/env python3
"""Modbus TCP cache + write-through proxy for Huawei SUN2000 inverters.

The SUN2000's Modbus TCP interface (via the SDongle) accepts only a very small
number of concurrent connections. Point Home Assistant, a heating controller and
a dashboard at it at the same time and it starts refusing connections and
dropping reads.

This proxy sits in front of it: it holds exactly ONE connection to the inverter,
polls a configured set of registers on an interval, and serves those cached
values to as many Modbus TCP clients as you like. Write requests (FC6) are
forwarded straight through to the inverter, so control still works.

Pure Python standard library — no dependencies.

Configuration is via environment variables:
    SUN2000_HOST        inverter / SDongle address        (required)
    SUN2000_PORT        inverter Modbus port              (default 502)
    SUN2000_UNIT_ID     Modbus unit / slave id            (default 1)
    LISTEN_HOST         address to serve on               (default 0.0.0.0)
    LISTEN_PORT         port to serve on                  (default 5502)
    POLL_INTERVAL       seconds between polls             (default 10)
    MAX_PV_KW           limit for PV counter growth, kW   (default 20)
    SESSION_INIT_DELAY  wait after connecting, seconds    (default 1.5)
    LOG_LEVEL           INFO / DEBUG / WARNING            (default INFO)
"""

import itertools
from datetime import datetime
import asyncio
import logging
import os
import signal
import struct
import time

# === Configuration (environment-driven) ===
SDONGLE_HOST = os.environ.get("SUN2000_HOST")
SDONGLE_PORT = int(os.environ.get("SUN2000_PORT", 502))
DEVICE_ID = int(os.environ.get("SUN2000_UNIT_ID", 1))

SERVER_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("LISTEN_PORT", 5502))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 10))
MAX_PV_KW = float(os.environ.get("MAX_PV_KW", 20))  # plausibility limit, above any real output
SESSION_INIT_DELAY = float(os.environ.get("SESSION_INIT_DELAY", 1.5))

if not SDONGLE_HOST:
    raise SystemExit(
        "SUN2000_HOST is not set.\n"
        "Point it at your inverter's SDongle, e.g.  SUN2000_HOST=10.0.0.50 python3 modbus_cache_server.py"
    )

# Register batches to read: (start_address, count).
# Defaults cover a SUN2000 with a LUNA2000 battery and a grid meter. Trim or
# extend to match your hardware — reading registers your inverter doesn't expose
# just logs a failed batch.
REGISTER_BATCHES = [
    (32016, 4),   # PV string 1/2 voltage + current
    (32064, 2),   # Input power (int32)
    (32069, 3),   # Phase A/B/C voltages
    (32080, 2),   # Active power (int32)
    (32085, 1),   # Grid frequency
    (32087, 1),   # Inverter temperature
    (32106, 2),   # Cumulative energy yield (uint32)
    (32114, 2),   # Daily energy yield (uint32)
    (37113, 2),   # Active grid power (int32)
    (37119, 4),   # Grid export (2) + grid import (2)
    (37760, 1),   # Battery SOC
    (37765, 2),   # Battery power (int32)
    (37780, 8),   # Battery total charge/discharge + daily
]

# === Logging ===
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("modbus_cache")

# === Register Cache ===
# Dict mapping register address -> uint16 value
register_cache = {}
cache_lock = asyncio.Lock()
last_update = 0
batch_updated = {}
yield_values = {}
pending_yield = {}
transaction_ids = itertools.count(0)
MAX_CACHE_AGE = 120
REBASE_READS = 3  # Consistent reads needed before a counter may move to a new baseline.


def next_transaction_id():
    """1..65535: the dongle sends an unsolicited frame with id 0 on every new connection."""
    return next(transaction_ids) % 65535 + 1


class InvalidResponse(Exception):
    """The connection cannot safely be used for another request."""


async def read_response(reader, transaction_id, unit_id, function, data_size):
    """Read exactly one validated Modbus ADU, even across TCP fragments."""
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        header = await asyncio.wait_for(reader.readexactly(7), timeout=remaining)
        tx, protocol, length, unit = struct.unpack(">HHHB", header)
        if protocol != 0 or not 2 <= length <= 254:
            raise InvalidResponse(f"Invalid Modbus header: {header.hex()}")
        pdu = await asyncio.wait_for(reader.readexactly(length - 1), timeout=max(.01, deadline-asyncio.get_running_loop().time()))
        if (tx, unit) == (transaction_id, unit_id):
            break
        logger.warning("Discarding unrelated Modbus reply tx=%s unit=%s; expected tx=%s unit=%s", tx, unit, transaction_id, unit_id)
    if pdu[0] == function | 0x80 and len(pdu) == 2:
        return None  # A complete exception response does not desynchronize TCP.
    if pdu[0] != function or len(pdu) != data_size:
        raise InvalidResponse("Unexpected Modbus function or response size")
    return pdu


def valid_yield(start, values, stamp=None):
    """Reject impossible PV counters before either HA instance sees them."""
    if start not in (32106, 32114):
        return True
    stamp = time.time() if stamp is None else stamp
    value = ((values[0] << 16) | values[1]) / 100
    day = datetime.fromtimestamp(stamp).date()
    if not 0 <= value <= (10_000_000 if start == 32106 else 250):
        return False
    previous = yield_values.get(start)
    if previous:
        old, old_stamp, old_day = previous
        if start == 32106 or day == old_day:
            # MAX_PV_KW is a generous upper limit; allow rounding as well.
            if value < old or value - old > 0.1 + MAX_PV_KW * (stamp - old_stamp) / 3600:
                return confirm_new_baseline(start, value, stamp, day)
    pending_yield.pop(start, None)
    yield_values[start] = (value, stamp, day)
    return True


def confirm_new_baseline(start, value, stamp, day):
    """Accept a rejected counter only after several consistent reads.

    A single wrong reply stays rejected. Without this, a wrong first read after a
    restart, or an inverter resetting its daily counter just after local midnight,
    would block the register until the value caught up again.
    """
    last = pending_yield.get(start)
    count = 1
    if last and 0 <= value - last[0] <= 0.1 + MAX_PV_KW * (stamp - last[1]) / 3600:
        count = last[2] + 1
    if count < REBASE_READS:
        pending_yield[start] = (value, stamp, count)
        return False
    logger.warning("PV counter at %s moved to new baseline %.2f", start, value)
    pending_yield.pop(start, None)
    yield_values[start] = (value, stamp, day)
    return True


def init_cache():
    """Uninitialized registers stay absent instead of inventing zero counters."""
    register_cache.clear()
    batch_updated.clear()
    yield_values.clear()
    pending_yield.clear()


# === SDongle Reader ===
async def read_batch(reader, writer, start, count):
    """Read a single register batch. Returns list of values or None."""
    tx = next_transaction_id()
    req = struct.pack(">HHHBBHH", tx, 0, 6, DEVICE_ID, 3, start, count)
    writer.write(req)
    await writer.drain()

    pdu = await read_response(reader, tx, DEVICE_ID, 3, 2 + count * 2)
    if pdu is None:
        return None
    if pdu[1] != count * 2:
        raise InvalidResponse("Unexpected register byte count")
    return struct.unpack(">" + "H" * count, pdu[2:])


async def read_sdongle():
    """Connect to SDongle and read all register batches in one session."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(SDONGLE_HOST, SDONGLE_PORT),
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"SDongle connect failed: {e}")
        return False

    success_count = 0
    fail_count = 0
    # The SDongle needs time to initialize its Modbus session after TCP connect.
    await asyncio.sleep(SESSION_INIT_DELAY)

    for start, count in REGISTER_BATCHES:
        try:
            values = await read_batch(reader, writer, start, count)
            if values:
                if not valid_yield(start, values):
                    logger.warning("Rejected implausible PV counter at %s", start)
                    fail_count += 1
                    continue
                async with cache_lock:
                    for i, val in enumerate(values):
                        register_cache[start + i] = val
                    batch_updated[start] = time.time()
                success_count += 1
            else:
                fail_count += 1
            await asyncio.sleep(0.05)  # 50ms between reads
        except asyncio.TimeoutError:
            fail_count += 1
            break  # Late replies must never be consumed by the next request.
        except Exception as e:
            logger.warning(f"Error reading {start}: {type(e).__name__}: {e}")
            fail_count += 1
            break  # Connection likely broken, exit loop

    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

    if success_count > 0:
        logger.info(f"Poll: {success_count}/{success_count + fail_count} batches OK")
    return success_count > 0


async def reader_loop():
    """Periodically read from SDongle and update cache."""
    global last_update

    # Try rapidly at first to fill cache, then slow down
    retry_delay = 5

    while True:
        success = await read_sdongle()
        if success:
            last_update = time.time()
            retry_delay = POLL_INTERVAL  # Normal interval after success
        else:
            retry_delay = min(retry_delay, 10)  # Retry faster on failure
            age = time.time() - last_update if last_update > 0 else -1
            if age > 120:
                logger.warning(f"Cache stale for {age:.0f}s")

        await asyncio.sleep(retry_delay)


# === Modbus TCP Server ===
async def handle_client(client_reader, client_writer):
    """Handle a single Modbus TCP client connection."""
    addr = client_writer.get_extra_info("peername")
    logger.info(f"Client connected: {addr}")

    try:
        while True:
            # Read MBAP header (7 bytes)
            header = await asyncio.wait_for(client_reader.readexactly(7), timeout=60)

            tx_id, proto, length, unit_id = struct.unpack(">HHHB", header)
            if proto != 0 or not 2 <= length <= 254:
                break

            # Read remaining PDU
            pdu = await asyncio.wait_for(client_reader.readexactly(length - 1), timeout=5)
            if len(pdu) < 1:
                break

            fc = pdu[0]

            if fc == 3 and len(pdu) >= 5:
                # FC3: Read Holding Registers
                reg_addr, reg_count = struct.unpack(">HH", pdu[1:5])
                if not 1 <= reg_count <= 125:
                    break

                # Serve from cache
                values = []
                async with cache_lock:
                    fresh = all(
                        key in register_cache and any(
                            start <= key < start + count
                            and time.time() - batch_updated.get(start, 0) <= MAX_CACHE_AGE
                            for start, count in REGISTER_BATCHES
                        )
                        for key in range(reg_addr, reg_addr + reg_count)
                    )
                    for i in range(reg_count):
                        addr_key = reg_addr + i
                        values.append(register_cache.get(addr_key, 0))
                if not fresh:
                    resp_pdu = bytes([0x83, 11])  # Gateway target unavailable.
                    client_writer.write(struct.pack(">HHHB", tx_id, 0, 3, unit_id) + resp_pdu)
                    await client_writer.drain()
                    continue

                # Build response
                byte_count = reg_count * 2
                resp_pdu = struct.pack(">BB", fc, byte_count)
                resp_pdu += struct.pack(">" + "H" * reg_count, *values)
                resp_header = struct.pack(">HHHB", tx_id, 0, len(resp_pdu) + 1, unit_id)
                client_writer.write(resp_header + resp_pdu)
                await client_writer.drain()

            elif fc == 6 and len(pdu) >= 5:
                # FC6: Write Single Register — pass through to SDongle
                reg_addr, reg_value = struct.unpack(">HH", pdu[1:5])
                logger.info(f"Write request from {addr}: register {reg_addr} = {reg_value}")

                # Forward to SDongle
                write_ok = await forward_write(tx_id, unit_id, reg_addr, reg_value)
                if write_ok:
                    # Echo back (standard Modbus write response)
                    resp_pdu = struct.pack(">BHH", fc, reg_addr, reg_value)
                    resp_header = struct.pack(">HHHB", tx_id, 0, len(resp_pdu) + 1, unit_id)
                    client_writer.write(resp_header + resp_pdu)
                else:
                    # Error response
                    resp_pdu = struct.pack(">BB", fc + 0x80, 4)  # Device failure
                    resp_header = struct.pack(">HHHB", tx_id, 0, len(resp_pdu) + 1, unit_id)
                    client_writer.write(resp_header + resp_pdu)
                await client_writer.drain()

            else:
                # Unsupported function code — return exception
                resp_pdu = struct.pack(">BB", fc + 0x80, 1)  # Illegal function
                resp_header = struct.pack(">HHHB", tx_id, 0, len(resp_pdu) + 1, unit_id)
                client_writer.write(resp_header + resp_pdu)
                await client_writer.drain()

    except asyncio.TimeoutError:
        pass  # Client idle timeout
    except ConnectionResetError:
        pass
    except asyncio.IncompleteReadError:
        pass
    except Exception as e:
        logger.warning(f"Client {addr} error: {type(e).__name__}: {e}")
    finally:
        logger.info(f"Client disconnected: {addr}")
        try:
            client_writer.close()
            await client_writer.wait_closed()
        except Exception:
            pass


async def forward_write(tx_id, unit_id, reg_addr, reg_value):
    """Forward a write request directly to the SDongle."""
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(SDONGLE_HOST, SDONGLE_PORT),
            timeout=10,
        )
        upstream_tx = next_transaction_id()
        req = struct.pack(">HHHBBHH", upstream_tx, 0, 6, unit_id, 6, reg_addr, reg_value)
        writer.write(req)
        await writer.drain()

        resp = await read_response(reader, upstream_tx, unit_id, 6, 5)
        if resp == struct.pack(">BHH", 6, reg_addr, reg_value):
            logger.info(f"Write forwarded OK: register {reg_addr} = {reg_value}")
            return True
        else:
            logger.warning(f"Write forward: short response ({len(resp)} bytes)")
            return False

    except Exception as e:
        logger.warning(f"Write forward failed: {type(e).__name__}: {e}")
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


# === Main ===
async def main():
    init_cache()

    # Start reader loop
    reader_task = asyncio.create_task(reader_loop())

    # Start Modbus TCP server
    server = await asyncio.start_server(handle_client, SERVER_HOST, SERVER_PORT)
    logger.info(f"Modbus Cache Server listening on {SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"Polling SDongle at {SDONGLE_HOST}:{SDONGLE_PORT} every {POLL_INTERVAL}s")
    logger.info(f"Caching {sum(c for _, c in REGISTER_BATCHES)} registers in {len(REGISTER_BATCHES)} batches")

    # Handle shutdown signals
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()

    logger.info("Shutting down...")
    reader_task.cancel()
    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
