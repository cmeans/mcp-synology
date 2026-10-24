# System Module — Tool Specifications

> **Version:** 0.6 | **Updated:** 2026-10-11. First spec for this module, written from the shipped code and its verified behavior (#137). Tool count: 2 READ.
> **Parent doc:** `docs/specs/architecture.md`
>
> Kept in sync with the code by `tests/test_module_specs.py`: tool headings, permission tiers, parameter names and the exact LLM-facing descriptions must match what ships.

## Module Metadata

```python
MODULE_INFO = ModuleInfo(
    name="system",
    description="Monitor Synology NAS system health: CPU, memory, disk, network, temperature",
    required_apis=[
        ApiRequirement(api_name="SYNO.DSM.Info", min_version=1),
        ApiRequirement(api_name="SYNO.Core.System", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.System.Utilization", min_version=1, optional=True),
    ],
    tools=[...],  # 2 READ tools, below
)
```

The module has no settings.

---

## Design Conventions

### Two sources, by privilege

| API | Who can call it | Used for |
|---|---|---|
| `SYNO.DSM.Info` / `getinfo` | Any DSM user | Model, DSM version, RAM, temperature (with its warning flag), uptime, system time |
| `SYNO.Core.System` / `info` (v1) | Admins | CPU series, cores and clock; NTP server. Also a fallback for model, firmware, RAM, temperature and uptime |
| `SYNO.Core.System.Utilization` / `get` | Admins | Live CPU, memory, network and disk I/O |

`get_system_info` merges the first two. A failure of either source (including a non-admin's refusal from `SYNO.Core.System`) is logged at DEBUG and that source is treated as empty, so a non-admin still gets the `DSM.Info` fields. The tool fails with `unavailable` when both are empty, or when the sources returned data but none of it produced a field.

`get_resource_usage` needs `SYNO.Core.System.Utilization`. A non-admin's 105 becomes a structured `permission_denied` error, and per the project rule it **never triggers a re-auth** (verified on vdsm with the non-admin persona and the re-auth spy, #124/#133).

### Formatting

Both tools return one `format_key_value` block. Absent fields are left out rather than shown as empty. Units:

- RAM in MB.
- Temperature in °C. `⚠ WARNING` is appended when DSM's `temperature_warn` is set. A reading of 0 °C is shown, not treated as missing.
- Uptime as `123d 3h 9m` (`< 1m` under a minute).
- Rates use the shared binary size formatter plus `/s`.
- CPU figures are DSM's own percentages and load values, unconverted.

### Errors

| Situation | `code` | Retryable |
|---|---|---|
| Neither info source returned data | `unavailable` | yes |
| `SYNO.Core.System.Utilization` isn't in the API cache | `api_not_found` | no |
| Utilization refused with 105 (non-admin) | `permission_denied` | no |
| Utilization answered but every metric was empty (e.g. right after boot) | `unavailable` | yes |
| Any other DSM error | mapped by `synology_error_response` | per code |

A spurious DSM 119 on `Utilization` for a non-admin has been seen on CI but not reproduced locally (#126). The core client treats 119 as a session error and re-authenticates once, which is the intended behavior for 119.

---

## READ Tier Tools (2 tools)

### 1. `get_system_info`

Hardware and software identity and basic health.

**Permission tier:** READ

**DSM API:** `SYNO.DSM.Info` / `getinfo`, plus `SYNO.Core.System` / `info` v1 when available

**Tool description (LLM-facing):**
> Get NAS hardware and software info: model, firmware, RAM, temperature, uptime, and for a DSM
> administrator account also the CPU. Use this to identify the NAS and check its basic health. For
> live CPU, memory, disk and network load, use get_resource_usage.

The CPU (and NTP) come from `SYNO.Core.System`, which only an administrator may call, so a
non-admin account's reply has no CPU line. The description said "CPU" unconditionally until the
0.10.0rc6 live test, where claude.ai on a non-admin connection found the line missing.

**Parameters:** None.

**Response shape** (admin account; a non-admin sees the same block without `CPU` and `NTP`):

```
System Info
===============================
  Model:        DS1618+
  Firmware:     DSM 7.1.1-42962 Update 9
  CPU:          C3538 (4 cores) @ 2100 MHz
  RAM:          4096 MiB
  Temperature:  49°C
  Uptime:       123d 3h 9m
  System time:  Sun Oct 11 21:54:52 2026 (NAS local time)
  NTP:          time.google.com
```

**Design notes:**
- Field precedence: `DSM.Info` first, then `Core.System`. Temperature tells a missing value from 0°C with explicit `None` checks. RAM and uptime are shown only when nonzero.
- `NTP` appears only when NTP is enabled and a server is set.
- `System time` is DSM's string as sent: the NAS's current local time, with no zone (seska, DSM 7.1.1: `Wed Oct 21 15:47:18 2026` at 20:47 UTC, NAS in CDT; `SYNO.DSM.Info` has no zone field). So it's labeled `(NAS local time)`.

---

### 2. `get_resource_usage`

Live utilization, for deciding whether the NAS is busy before an expensive operation.

**Permission tier:** READ

**DSM API:** `SYNO.Core.System.Utilization` / `get` (admin)

**Tool description (LLM-facing):**
> Get live resource utilization: CPU load, memory usage, disk I/O, and network throughput. Use this to
> check if the NAS is under heavy load before starting expensive work on it. For the model, firmware,
> temperature and uptime, use get_system_info.

**Parameters:** None.

**Response shape:**

```
Resource Usage
===================================
  CPU load avg:     1m=0.81  5m=1.06  15m=1.21
  CPU usage:        17% (system=7%, user=10%)
  Memory usage:     74%
  Memory detail:    4096 MiB total, 171 MiB available, 824 MiB cached
  Swap in:          1 pages/s
  Network (total):  ↓ 296.3 KiB/s  ↑ 457.8 KiB/s
  Network (eth0):   ↓ 15.4 KiB/s  ↑ 73.8 KiB/s
  Disk (sda):       1% busy  R=135.2 KiB/s  W=234.8 KiB/s
```

**Design notes:**
- `CPU load avg` appears only when DSM reports the 1/5/15-minute loads. DSM reports them ×100 (`1min_load: 81` is a load of 0.81), so they're divided by 100 and shown to two decimals (#147). Otherwise, `CPU usage` comes from `system_load + user_load`, falling back to `other_load`.
- One `Network (<device>)` line per interface DSM reports with traffic, including its `total` pseudo-device; an idle interface (rx = tx = 0) is skipped. One `Disk (<device>)` line per disk with activity (busy %, or a read or write rate), with the rates when DSM includes them; an idle disk is skipped.
- DSM returns `disk` either as a list or as `{"disk": [...]}`; both are accepted.

---

## Resolved Design Decisions

### 1. Degrade, don't fail, for non-admins ✓

`get_system_info` keeps working for any user by treating the admin-only source as optional. `get_resource_usage` has no non-admin source, so it says plainly that an admin account is needed rather than returning partial data.

## Remaining Open Items

- **#126:** the occasional 119 on `Utilization` for a non-admin on CI.
