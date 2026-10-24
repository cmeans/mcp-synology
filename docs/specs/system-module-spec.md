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

### Sources, by privilege

Captured on vdsm (DSM 7.1, 7.2.2, 7.3.2, 7.4.1) as admin and as the non-admin test user, and on a DS1618+ (DSM 7.1.1); the captures are `tests/modules/system/fixtures/`.

| API | Who can call it | Used for |
|---|---|---|
| `SYNO.DSM.Info` / `getinfo` | Any DSM user | Model, serial number, DSM version string and build, RAM, temperature (with its warning flag), uptime (seconds), system time, code page |
| `SYNO.Core.System` / `info` (v1) | Admins (others get 1006) | CPU vendor, family, series, cores and clock; NTP on/off and server; time zone (`time_zone`, `time_zone_desc`); firmware date; redundant power (`support_rp`, `rp1`, `rp2`); eSATA; USB, SATA and PCIe devices. Also a fallback for model, serial, firmware, RAM, temperature (with three more warning flags) and uptime (`h:m:s`) |
| `SYNO.Core.Region.NTP` / `get` (v1) | Admins (others get 105) | The NAS clock as fields plus an exact `timestamp`, the zone name and NTP server |
| `SYNO.Core.System.Status` / `get` (v1) | Admins (others get 105) | `upgrade_ready`, `is_system_crashed`, and from DSM 7.2.2 `ext_nic_is_incompatible` |
| `SYNO.Core.System.Utilization` / `get` | Admins (others get 105) | Live CPU, memory, network and disk I/O (`get_resource_usage`) |

`get_system_info` shows **every field the first four send** (maintainer, 2026-10-23: "We should be showing all information that's being provided. Even Serial #."). Known fields get their own label. Any other field is listed under its own name, so a field a newer DSM adds is shown, not dropped. A failure of a source (including a non-admin's refusal) is logged at DEBUG and that source is treated as empty. `Region.NTP` and `System.Status` are asked only when `Core.System` answered, so another account doesn't spend two more refused requests. The tool fails with `unavailable` only when `DSM.Info` and `Core.System` are both empty.

- **CPU:** the non-empty parts of `cpu_vendor`, `cpu_family` and `cpu_series`, so it's "INTEL Atom C3538" on a DS1618+ and "AMD Ryzen 9 7940HS" on VirtualDSM, which puts the whole name in `cpu_vendor` and leaves the other two empty. Before 0.10.0 only `cpu_series` was read, so VirtualDSM's admin got no CPU line.
- **Time zone:** `time_zone_desc` without its `(GMT±hh:mm)` prefix (plus `time_zone` when the description doesn't contain it), then the offset in force now: the difference between `Region.NTP`'s clock fields and its `timestamp`, rounded to 15 minutes. The prefix is the zone's *standard* offset: the DS1618+ said `GMT-06:00` while on daylight time (UTC-05:00). The vdsm images give UTC-05:00 (Central, daylight time), +00:00, +05:30 and +02:00 (Amsterdam, summer time).
- **Other accounts:** `Time zone: not available to this account (needs a DSM administrator)`, and a `Needs a DSM administrator` row naming the admin-only details. The tool doesn't estimate the zone from the clock (maintainer decision, 2026-10-23: say it plainly).
- **Serial number** is shown (it was left out before 0.10.0).

The description listed only some of this until the 0.10.0rc6 live test and its QA round: claude.ai on a non-admin connection found the promised CPU line missing, and nothing tied "what time is it on the NAS?" to this tool.

`get_resource_usage` needs `SYNO.Core.System.Utilization`. A non-admin's 105 becomes a structured `permission_denied` error, and per the project rule it **never triggers a re-auth** (verified on vdsm with the non-admin persona and the re-auth spy, #124/#133).

### Formatting

Both tools return one `format_key_value` block. Absent fields are left out rather than shown as empty. Units:

- RAM in MiB.
- Temperature in °C. `⚠ WARNING` is appended when DSM's `temperature_warn` is set. A reading of 0 °C is shown, not treated as missing.
- Uptime as `123d 3h 9m` (`< 1m` under a minute).
- Rates use the shared binary size formatter plus `/s`.
- CPU figures are DSM's own percentages and load values, unconverted.

### Errors

| Situation | `code` | Retryable |
|---|---|---|
| Neither `DSM.Info` nor `Core.System` returned data | `unavailable` | yes |
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
> Get everything DSM reports about the NAS. Every account gets the model, serial number, DSM
> version, RAM, temperature, uptime and the NAS's local time. A DSM administrator account also gets
> the CPU, time zone with the current UTC offset, NTP server, firmware date, whether a DSM update is
> ready to install, and attached devices; any other account gets a line saying so. Use this to
> identify the NAS, check its basic health or tell the time on it. For live CPU, memory, disk and
> network load, use get_resource_usage.

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
> Get live resource utilization: CPU load, memory usage, disk I/O, and network throughput. Needs a
> DSM administrator account; any other account gets permission_denied. Use this to check if the NAS
> is under heavy load before starting expensive work on it. For the model, firmware, temperature and
> uptime, use get_system_info.

The admin requirement was missing from the description until the same 0.10.0rc6 class sweep that
found `get_system_info`'s CPU line: a non-admin connection's model had no way to know the call
would be refused.

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
