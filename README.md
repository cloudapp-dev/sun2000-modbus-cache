# sun2000-modbus-cache

**A Modbus TCP cache and write-through proxy for Huawei SUN2000 inverters.**
One connection to the inverter, as many clients as you want. No dependencies.

## The problem

The SUN2000's Modbus TCP interface — whether you reach it through the SDongle or
a direct connection — accepts only a **very small number of concurrent
connections**. That limit is easy to hit without realising it:

- Home Assistant polling the inverter for PV and battery sensors
- a surplus-heating controller (my·PV AC·THOR, a wallbox, a heat pump) reading
  grid feed-in to decide when to run
- a dashboard, a script, or a second Home Assistant instance

Three clients is often already too many. The symptoms are recognisable: reads
that time out at random, sensors that go `unavailable` for a minute and come
back, a heating controller that stops seeing surplus, and inverter connections
that get refused entirely until something disconnects. Nothing is broken —
you've simply run out of Modbus sockets.

The usual advice is "poll less often", which trades away exactly the
responsiveness you installed the integration for.

## What this does

This proxy sits between your clients and the inverter:

```
  Home Assistant  ┐
  AC·THOR         ├──►  sun2000-modbus-cache  ──►  SUN2000 / SDongle
  dashboard       ┘         (one connection)
  …any number
```

- Holds **exactly one** connection to the inverter and polls a configured set of
  registers on an interval (default every 10 s).
- Serves those cached values to **any number** of Modbus TCP clients (FC3, read
  holding registers) — instantly, with no load on the inverter.
- **Forwards writes** (FC6, write single register) straight through to the
  inverter, so control paths keep working.
- Keeps serving the last known values when the inverter is briefly unreachable,
  instead of failing every client at once. Stale caches are logged.

Because reads are served from memory, a client can poll as fast as it likes
without the inverter ever noticing.

## Requirements

Python 3.8+. **No third-party packages** — standard library only.

## Quick start

```bash
git clone https://github.com/cloudapp-dev/sun2000-modbus-cache.git
cd sun2000-modbus-cache
SUN2000_HOST=10.0.0.50 python3 modbus_cache_server.py
```

Then point your clients at this machine on port `5502` instead of the inverter
on `502`.

### Docker

```bash
docker compose up -d   # edit SUN2000_HOST in docker-compose.yml first
```

### systemd

```bash
sudo cp modbus_cache_server.py /opt/sun2000-modbus-cache/
sudo cp sun2000-modbus-cache.service /etc/systemd/system/
sudo systemctl enable --now sun2000-modbus-cache
```

## Configuration

All configuration is via environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `SUN2000_HOST` | *(required)* | Inverter / SDongle address |
| `SUN2000_PORT` | `502` | Inverter Modbus port |
| `SUN2000_UNIT_ID` | `1` | Modbus unit / slave id |
| `LISTEN_HOST` | `0.0.0.0` | Address to serve on |
| `LISTEN_PORT` | `5502` | Port to serve on |
| `POLL_INTERVAL` | `10` | Seconds between inverter polls |
| `MAX_PV_KW` | `20` | Limit for PV counter growth in kW, set it above your real output |
| `SESSION_INIT_DELAY` | `1.5` | Seconds to wait after connecting before the first request |
| `LOG_LEVEL` | `INFO` | `DEBUG` for per-batch detail |

### Registers

The polled registers are the `REGISTER_BATCHES` list at the top of
`modbus_cache_server.py`. The defaults cover a SUN2000 with a LUNA2000 battery
and a grid meter: PV strings, input and active power, phase voltages, grid
frequency, inverter temperature, daily and cumulative yield, grid import/export,
battery SOC, battery power, and battery charge/discharge totals.

Trim the list to what your hardware actually exposes — requesting registers your
inverter doesn't have simply logs a failed batch, it won't stop the proxy.

## Home Assistant

Point the built-in Modbus integration at the proxy instead of the inverter:

```yaml
modbus:
  - name: huawei_inverter
    type: tcp
    host: 10.0.0.60      # the machine running this proxy
    port: 5502
    sensors:
      - name: PV Power
        address: 32080
        input_type: holding
        data_type: int32
        unit_of_measurement: W
        device_class: power
      - name: Battery SOC
        address: 37760
        input_type: holding
        data_type: uint16
        scale: 0.1
        unit_of_measurement: "%"
        device_class: battery
```

The same applies to the Huawei Solar HACS integration and to any other Modbus
client — they all just talk to a different host and port.

## Notes and limitations

- **Read values are as fresh as `POLL_INTERVAL`.** That is the trade: every
  client gets an instant answer, at the cost of data up to one poll interval old.
  For PV and battery telemetry that's fine; for sub-second control loops it isn't.
- **Writes are not cached** — they go straight to the inverter and are subject
  to whatever the inverter accepts. If your inverter refuses a write, this proxy
  won't change that.
- **No authentication.** It's a plain Modbus TCP server, exactly like the
  inverter's own. Keep it on a trusted network segment.
- Only FC3 (read holding registers) and FC6 (write single register) are
  implemented; anything else returns an "illegal function" exception.

## Background

The reasoning, the failure modes and the register map are written up in more
detail here:

- [Caching a Huawei SUN2000 over Modbus](https://www.cloudapp.dev/caching-huawei-sun2000-modbus-home-assistant)
- [The SUN2000 Modbus registers I actually use](https://www.cloudapp.dev/home-assistant-huawei-sun2000-modbus-registers)
- [Write-through on a Modbus proxy](https://www.cloudapp.dev/home-assistant-modbus-write-through-proxy)
- [Reconnects and stale caches](https://www.cloudapp.dev/home-assistant-modbus-proxy-reconnect-stale-cache)

Built and run in production on a ~9 kWp SUN2000 with a LUNA2000 battery, serving
Home Assistant and an AC·THOR surplus heater at the same time.

## License

MIT — see [LICENSE](LICENSE).
