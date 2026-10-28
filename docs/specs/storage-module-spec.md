# Storage Module — Tool Specifications

> **Version:** 0.1 (draft for review) | **Updated:** 2026-10-28. Written before the code (#49) from live request and reply captures. Phase 1 tool count: 11 READ. Phase 2 (reversible WRITE tools) is planned below and gets its own spec revision.
> **Parent doc:** `docs/specs/architecture.md`
>
> **Verified on:** vdsm DSM 7.1, 7.2.2, 7.3.2 and 7.4.1 (admin and the non-admin test user), and a real NAS: a DS1618+ on DSM 7.1.1 Update 9 with four HDDs in a RAID 5 pool, a hot spare, an SSD read cache and Snapshot Replication installed (admin, read-only). Request shapes come from DSM's own webapi definitions (`SYNO.Storage.CGI.lib`, `SYNO.Core.Storage.lib`, …: methods and who may call them) and Storage Manager's web client (`storage_panel.js`, `storage_wizard.js`: parameters), then each call was sent and its reply recorded. A claim marked **verified** has a capture in the fixtures; **inferred** marks a reading of evidence that no capture proves outright.
>
> **Fixtures:** `tests/modules/storage/fixtures/dsm-<version>-vdsm.json` (vdsm) and `dsm-7.1.1-ds1618plus-realnas.json` (the real NAS, scrubbed with `scripts/scrub_nas_capture.py`: serial numbers and other identifiers (`unique_key`) become `FAKE…`, shared-folder names `share01`… and their descriptions `Description 01`… (in capture-label keys too), UUIDs fakes of the same format, the hostname `NAS01`, LAN and Tailscale addresses `192.0.2.x`; `tests/scripts/test_scrub_nas_capture.py` checks every `*-realnas.json` fixture and fails if anything in it still looks real). The successful `Volume enum_resource` capture (an excluded pre-delete check) keeps its request but not its data, which lists every share and package storage on the volume. Keys are capture labels; each value holds the request as sent and DSM's reply.
>
> Kept in sync with the code by `tests/test_module_specs.py` once the module is registered: tool headings, permission tiers, parameter names and the exact LLM-facing descriptions must match what ships.

## Scope

Maintainer decisions, 2026-10-28:

- **In this module:** Storage Manager (storage pools, volumes, disks and S.M.A.R.T., data scrubbing, hot spares, SSD cache, TRIM, space reclamation, Btrfs deduplication, health thresholds and repair settings), external USB and eSATA storage and expansion units, share snapshots (`SYNO.Core.Share.Snapshot`), and DSM's system health summary.
- **Separate modules later:** iSCSI LUNs and SAN Manager, Task Scheduler, Hyper Backup.
- **Phases:** phase 1 is the READ tools in this document; phase 2 adds the reversible WRITE tools (planned below).
- **Excluded, with the reason:** operations that wipe disks or change a pool's layout: creating, deleting, expanding, migrating (RAID type change) or repairing pools and volumes, `secure_erase`, deactivating a disk, KMIP `erase_all_data`, assembling or removing detected pools, and encryption-key operations. Each can destroy all data on the NAS, and none can be tested: vdsm has no disks or pools (below), and the only real NAS available holds real data. The project's whole-API rule allows an exclusion for a stated, specific reason; these are it. `SYNO.Storage.CGI.Volume enum_resource` and `pre_delete_check` (what a volume's deletion would affect) go with them.

## Module Metadata

```python
MODULE_INFO = ModuleInfo(
    name="storage",
    description="Storage pools, volumes, disks and their health, SSD cache, external storage, snapshots",
    required_apis=[
        ApiRequirement(api_name="SYNO.Storage.CGI.Storage", min_version=1),
        ApiRequirement(api_name="SYNO.Storage.CGI.Smart", min_version=1),
        ApiRequirement(api_name="SYNO.Core.Storage.Disk", min_version=1),
        ApiRequirement(api_name="SYNO.Storage.CGI.Volume", min_version=1),
        ApiRequirement(api_name="SYNO.Storage.CGI.Pool", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Smart.Scheduler", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Spare", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Spare.Conf", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.HddMan", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Flashcache", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.BtrfsDedupe", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Storage.CGI.Check", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.ExternalDevice.Storage.USB", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.ExternalDevice.Storage.eSATA", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.ExternalDevice.Storage.EUnit", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.ExternalDevice.Storage.Setting", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.Share", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Core.Share.Snapshot", min_version=2, optional=True),
        ApiRequirement(api_name="SYNO.Core.System.SystemHealth", min_version=1, optional=True),
    ],
    tools=[...],  # phase 1: 11 READ tools, below
)
```

The module has no settings in phase 1. It's opt-in like the others: added under `modules:` as `storage`.

---

## Design Conventions

### Who can use it

**Administrators only.** Every API here declares `allowUser: ["admin.local", "admin.ldap", "admin.domain"]` in DSM's webapi definitions on every version (read from the `.lib` files over SSH on each vdsm image; not in the fixtures). The non-admin test user's in-scope read calls on DSM 7.2.2 all answered **105** except one (verified): `Core.ExternalDevice.Storage.USB list` answers **404** to a non-admin on 7.2.2, 7.3.2 and 7.4.1 and 105 on 7.1, while eSATA and EUnit answer 105 on all four (verified). An admin gets success from all three. So `list_external_storage` treats a 404 from `USB list` as the same refusal as 105.

So a non-admin connection that enables the module gets `permission_denied` from every tool, which per the project rule never triggers a re-auth. The README and the server instructions say the module needs an administrator account. Registering the tools anyway (rather than hiding them) matches how Download Station's manager-only tools behave: the error says what's needed.

### Sources

`SYNO.Storage.CGI.Storage` / `load_info` is the one call Storage Manager's own UI builds its pages from: one reply holds `disks`, `storagePools`, `volumes`, `ssdCaches`, `sharedCaches`, `spares` (in each pool), `detected_pools`, `missing_pools`, `ports`, `env` (model, bay count, expansion units, scrub schedule state) and `overview_data` (overall status level). `ssdCaches` and `sharedCaches` are absent, not empty, when the NAS has no SSD cache (verified on all four vdsm versions), so they're read as optional. `get_storage_overview`, `list_storage_pools`, `list_volumes`, `get_volume`, `list_disks`, `get_disk` and `list_ssd_caches` read it; `get_storage_schedules`, `get_storage_settings`, `list_external_storage` and `list_snapshots` don't. Detail tools add per-object calls.

`SYNO.Core.Storage.Disk list`, `SYNO.Core.Storage.Pool list` and `SYNO.Core.Storage.Volume list`/`get` answer **101** to a plain call on every version and on the real NAS (verified; first reported by Hoyass on DSM 7.3.2 in #49), and Storage Manager's UI doesn't call them. They aren't used.

| Area | Calls (all v1 unless noted) | Verified |
|---|---|---|
| Overview | `Storage.CGI.Storage load_info`, `load_bad_disks`, `get_sche_task_list`; `Storage.CGI.Check is_data_scrubbing`, `is_building`; `Core.System.SystemHealth get` | all versions + real NAS |
| Disks | `load_info`'s `disks`; `Storage.CGI.Smart get_health_info {device}`, `get_smart_info {device}`, `smart_warning_get {device}`; `Core.Storage.Disk get_smart_test_log {device}`, `disk_test_log_get {device, offset, limit, sort_by: "time", sort_direction: "DESC"}`, `get_performance_test_status {device}`, `wcache_get {device}`; `Core.Storage.Disk disk_health_prediction_log_get` (7.1 vdsm and the real NAS: `{}`) | real NAS (vdsm has no disks) |
| Pools | `load_info`'s `storagePools`; `Storage.CGI.Pool get_setting {pool_path}` (the pool's `space_path`, e.g. `/dev/vg1`, not its `pool_path` field); `Storage.CGI.Spare list`, `potential_spares_dump`; `Spare.Conf get` | real NAS |
| Volumes | `load_info`'s `volumes`; `Storage.CGI.Volume get_space_usage {volume_path}`, `ssd_trim_get {space_path}`; `BtrfsDedupe get_volume_info {vol_paths: [..]}`; `Storage get_space_reclaim_status` | all versions + real NAS (dedupe: 121 there) |
| SSD cache | `load_info`'s `ssdCaches`, `sharedCaches`; `Flashcache shared_cache_config_get {shared_cache}`, `advisor_history_get` | real NAS |
| Schedules | `Storage get_schedule_plan` (scrubbing), `get_space_reclaim_schedule`, `get_sche_task_list`; `Smart.Scheduler list`; `BtrfsDedupe get_schedule_plan` | all versions + real NAS (dedupe: 121 there) |
| Settings | `Storage get_resync_speed`, `get_fast_repair_config`, `get_auto_repair_config`; `HddMan get`; `Spare.Conf get`; `Smart get_latest_online_drive_db_info` (7.2.2+); `Core.ExternalDevice.Storage.Setting get` | all versions + real NAS |
| External | `Core.ExternalDevice.Storage.USB list {additional: ["all"]}`, `.eSATA list`, `.EUnit list` | all versions without `additional` (empty lists); `additional: ["all"]` and a device only on the real NAS (a USB stick; no eSATA device or expansion unit) |
| Snapshots | `Core.Share list {additional: ["vol_path", "enable_snapshot", "snapshot_count"]}` (the count comes back as `snapshot_info.snapshot_count`; `enable_snapshot` doesn't come back); `Core.Share.Snapshot list` **v2** `{name, additional, offset, limit}`, `get_schedule {name}` | all four vdsm versions with a real snapshot (bare `Core.Share list`); real NAS (no snapshots; `additional` verified there) |

### Request encoding

Every API here reports `requestFormat=JSON`, so DSM parses each parameter value as JSON (as File Station's do, #121). **A list must be sent JSON-encoded**: `space_id=["volume_1"]` as a JSON string works, while the same list passed as a Python list (which httpx sends as a repeated plain `space_id=volume_1`) gets **101** (verified on the real NAS: `Volume enum_resource` with `space_id` as a plain list answers 101 and as a JSON string succeeds; the success's data is left out of the fixture for privacy). Free-text values (a share name) are JSON-quoted with `dsm_str()`. An unquoted `name=writable` also worked on vdsm (verified), but a share named `2024` or `true` would be read as a number or a boolean, as with File Station's folder names (#121); paths (`/dev/sda`, `/volume1`, `/dev/vg1`) start with `/` and need no quoting.

**Transport:** GET for every call. All are reads with short, non-secret parameters (device paths, volume paths, share names), so no access-log or URL-length concern applies; GET is what the captures used.

### Version differences (verified)

Method presence comes from each image's webapi `.lib` files and the captures; Storage Manager's package versions from each image's `/var/packages/StorageManager/INFO` (read over SSH, not in the fixtures).

| | 7.1 | 7.2.2 | 7.3.2 | 7.4.1 |
|---|---|---|---|---|
| Storage Manager | built into DSM | package 1.0.0-00502 | package 1.0.1-1100 | package 1.1.0-2018 |
| `Smart get_latest_online_drive_db_info` | absent | ✓ | ✓ | ✓ |
| `Core.Storage.Disk health_report_get`, `wcache_get` (no device) | absent | ✓ | ✓ | ✓ |
| `Core.Storage.Disk disk_config_get` | ✓ (vdsm 7.1; 103 on DSM 7.1.1) | absent | absent | absent |
| `Volume get_dump_volumes` | absent | ✓ | ✓ | ✓ |
| `Storage.CGI.VolumeReduction` (new API) | — | — | — | present; captured only without parameters (120; open item 3) |
| `is_encrypted`, `is_locked`, `is_vault_contain_key` in `load_info.volumes[]` | absent (also on the real NAS) | ✓ | ✓ | ✓ |
| `worm_lock` in a v2 snapshot | absent (`desc`, `lock`, `schedule_snapshot`, `time` only) | ✓ | ✓ | ✓ |
| `Smart get_health_info` without `device` | 117 (also the real NAS) | 114 | 114 | 114 |
| `Storage load_eunit_topology` | — | — | — | present (117 without an expansion unit) |
| `BtrfsDedupe get_volume_info` `has_snapshot` field | absent | ✓ | ✓ | ✓ |

The real NAS (DSM 7.1.1) answered every call the 7.1 vdsm did except the `BtrfsDedupe` methods (121 from each: deduplication not supported on the DS1618+; below) and `Core.Storage.Disk disk_config_get` (103, method doesn't exist on 7.1.1 though vdsm's 7.1 has it). It also answered the disk-level calls vdsm can't (below).

### vdsm has no disks

`load_info` on vdsm reports **0 disks, 0 pools** and one `basic` volume on `/dev/sdb1` (verified, all four versions): VirtualDSM's storage is a virtual block device, not a Synology pool. So disk, S.M.A.R.T., RAID, hot-spare and SSD-cache replies come only from the real NAS, and those code paths are tested against its (anonymized) fixture. vdsm tests cover the overview, volumes, schedules, settings, external storage, snapshots (with Snapshot Replication installed, below), the empty-disk and empty-pool paths, and the non-admin 105.

### Snapshots: `list` v1 hides snapshots

`SYNO.Core.Share.Snapshot list` **v1 answers `{"snapshots": [], "total": 0}` even when the share has a snapshot**; v2 lists it (verified on all four vdsm versions: one snapshot created on `writable`, v1 total 0, v2 total 1). The tool pins v2 (`negotiate_version(..., min_version=2)`).

- The parameter is `name` (the share name, JSON-quoted). Without it the call answers **403** (verified), which is what made Snapshot Replication look missing during the first sweep.
- A share that doesn't exist answers **402** from `list` (verified), but `get_schedule` **reports success** with a default, disabled schedule for it (verified). So the tool checks the share name against `Core.Share list` first, and never reports a schedule for a share that isn't there.
- `list` and `create` work with Snapshot Replication installed but its service stopped (vdsm; inferred to need only the package's APIs). vdsm images don't include the package; the vdsm tests that need a snapshot install it at test time or skip (open item 2).
- `get_share_conf` answers 403 even with `name` (real NAS, verified); its parameters aren't known yet. It's not used in phase 1.

### Unknown IDs

Each per-object call fails differently for an object that doesn't exist (verified on the real NAS):

| Call | Unknown object | Answer |
|---|---|---|
| `Smart get_health_info`, `get_smart_info`; `Core.Storage.Disk get_smart_test_log` | `/dev/sdz` | 117 |
| `Core.Storage.Disk disk_test_log_get` | `/dev/sdz` | 101 |
| `Volume get_space_usage` | `/volume9` | 48640 |
| `Volume ssd_trim_get` | `/volume9` | 117 |
| `Share.Snapshot list` v2 | `no-such-share` | 402 |
| `Share.Snapshot get_schedule` | `no-such-share` | **success** (a default schedule) |

So every tool that takes an ID resolves it against `load_info` (disks, pools, volumes, caches) or `Core.Share list` (shares) **before** any per-object call, and an unknown ID is a `not_found` error that lists the valid ones. The DSM codes above are then only reachable through a race (an object removed between the two calls) and map to `dsm_error` with the code.

### Feature not supported: 121

**121** looks like "this feature isn't available here" (inferred, from two cases): every `BtrfsDedupe` method answers 121 on the DS1618+ while `get_volume_info` answers `support_dedupe: true` on all four vdsm versions (verified); and `Cache.Protection` answers 121 on all four vdsm versions and on the real NAS, which does have an SSD cache, a read-only one (verified). Why the real NAS refuses `Cache.Protection` isn't known: it may apply only to read-write caches (unverified). The tools don't depend on the reason: a 121 from an optional section leaves that section out with a note (`Deduplication: not available on this NAS`); it never fails the tool.

114 and 120 were seen only from calls sent without their parameters during the sweep (inferred: a required parameter missing or invalid). **117 is also a missing or wrong parameter, not only an unknown ID:** `disk_test_log_get` with no device answers 117 on all five captures, `Flashcache shared_cache_config_get` with no parameter answers 117 on 7.2.2+ and with a wrong parameter name on the real NAS, and `Smart get_health_info` with no device answers 117 on 7.1 and the real NAS but 114 on 7.2.2+ (verified). The tools always send the right parameters, so any of 114, 117 or 120 after the ID check is a bug or a race and maps to `dsm_error` with the code.

### What's shown

Every field DSM sends is shown (maintainer, 2026-10-23, for `get_system_info`: "We should be showing all information that's being provided. Even Serial #."). Known fields get labels and units; any other field is listed under its own name, so a field a newer DSM adds isn't dropped. Serial numbers are shown. UI-only fields (`show_*`, `*_btn*`, `i18nNamingInfo`, `can_do`, `vspace_can_do`, `suggestions`' i18n keys) are left out of list tools and shown raw only by the `get_*` tools (open item 4: confirm with QA that this is the right line).

### Formatting

Shared formatters only (`format_table` for lists, `format_key_value` for one object, `format_status` for state lines). Sizes come as byte strings and use the shared binary size formatter; percentages are computed from DSM's byte counts, not rounded by DSM. Temperatures in °C. DSM's local-time strings are the NAS's own clock, shown as they are; they come in two formats: S.M.A.R.T. test times as `2026/10/28 04:00:02` (slashes, seconds) and the scheduler's `next_trigger_time` as `2026-10-29 04:00` (dashes, no seconds) (verified) (the server instructions' TIMES rule). **DSM's epoch times aren't reliable during daylight saving time:** on the real NAS (zone Central, clock at UTC-5 by `Region.NTP get` on 2026-10-28), `Smart.Scheduler list` gave the next quick test as `2026-10-29 04:00` local (09:00 UTC), while `get_sche_task_list` gave the same task's `next_run_time` as 10:00 UTC (verified). DSM appears to convert with the zone's standard offset (inferred; open item 7). So where DSM sends both, the tools show the local string; an epoch with no local counterpart (scrubbing's `next_start_time`, a pool's `last_done_time`) is shown in UTC, marked approximate during daylight saving time until open item 7 is settled. `-1` in `remain_life` and `unc` means "not reported by this drive" (verified: HDDs send `remain_life: -1`, the SSD sends `unc: -1`) and is shown as `—`.

### Errors

| Situation | `code` | Retryable |
|---|---|---|
| A non-admin account (105) | `permission_denied`, naming the administrator requirement | no |
| An unknown disk, pool, volume, cache or share | `not_found`, listing the valid IDs | no |
| A feature the NAS doesn't support (121), in a required section | `not_supported` | no |
| Storage Manager not answering (`SYNO.Storage.CGI.Storage` missing from the API cache) | `api_not_found` | no |
| Any other DSM error | mapped by `synology_error_response` | per code |

---

## READ Tier Tools (11 tools)

### 1. `get_storage_overview`

The answer to "is anything wrong with my storage?".

**Permission tier:** READ

**DSM API:** `Storage.CGI.Storage load_info`, `load_bad_disks`, `get_sche_task_list`; `Storage.CGI.Check is_data_scrubbing`, `is_building`; `Core.System.SystemHealth get`

**Tool description (LLM-facing):**
> Get the overall health of the NAS's storage: DSM's own status summary, every storage pool,
> volume, disk and SSD cache with its status in one line each, anything that needs attention
> (a degraded pool, a failing or incompatible disk, a nearly full volume) and storage tasks that
> are running or scheduled next (data scrubbing, S.M.A.R.T. tests, RAID rebuilds). Needs a DSM
> administrator account. Start here for any question about disks, RAID or free space; for one
> object's details use get_disk, get_volume or list_storage_pools.

**Parameters:** None.

**Response shape**, the DS1618+ (DSM 7.1.1; serials replaced):

```
Storage Overview — attention
==================================================
  System health:   storage needs attention (storage_is_attention)
  Model:           DS1618+, 6 bays, no expansion units

Needs attention
  - Storage pool 1 (reuse_1): attention, fs_almost_full
  - Volume 1 (/volume1): attention, fs_almost_full, 96.5% used

Storage pools
  Pool 1 (reuse_1)   RAID 5, 4 disks + 1 hot spare   attention   38.2 TiB

Volumes
  Volume 1 (/volume1)   Btrfs   attention   35.4 TiB of 36.6 TiB used (96.5%)

Disks
  Drive 1 (sda)   WD141KFGX-68FH9N0   12.7 TiB   38°C   normal   pool 1
  Drive 2 (sdb)   WD142KFGX-68AFPN0   12.7 TiB   41°C   normal   pool 1   extended S.M.A.R.T. test 90%, not on Synology's compatibility list
  Drive 3 (sdc)   WD141KFGX-68FH9N0   12.7 TiB   39°C   normal   pool 1
  Drive 4 (sdd)   WD161KRYZ-01AGBB0   14.6 TiB   41°C   normal   pool 1   extended S.M.A.R.T. test 90%
  Drive 5 (sde)   WD142KFGX-68AFPN0   12.7 TiB   39°C   normal   hot spare, not on Synology's compatibility list
  Drive 6 (sdf)   SSD 870 EVO 1TB   931.5 GiB   42°C   normal   SSD cache 1, 67% life left, not on Synology's compatibility list

SSD caches
  Cache 1 (shared_cache_1)   read-only, 1 SSD   for volume 1   hit rate 83%

Storage tasks
  Running:  S.M.A.R.T. extended test (drives 2 and 4 at 90%)
  Next:     S.M.A.R.T. quick test of 6 disks, 2026-10-29 04:00 (NAS time)
            Data scrubbing of pool 1, about 2026-12-18 06:00 UTC
```

The headline status is `overview_data.status_level`, which DSM 7.1 vdsm sends bare (`normal`) and the others prefixed (`status_level_normal`; the real NAS on 7.1.1 sent `status_level_abnormal`); both forms map the same way: `normal` → `normal`, `abnormal` → `attention`, `danger` → `danger` (verified: the two forms; `danger` inferred from `sample_level_str`). "Needs attention" lists each pool, volume and cache whose `summary_status` isn't `normal`, with its `space_status.detail`, and each disk whose `overview_status` isn't `normal` or that `load_bad_disks` lists as `unhealthy` or `not_compatible`. A disk's `summary_status_key` isn't an alarm by itself: `smart` while a S.M.A.R.T. test runs and `hotspare` for a spare (verified), so it shows on the disk's line, not under Needs attention. The same goes for `compatibility: "not_in_support"` (three of the real NAS's six disks, while `load_bad_disks.not_compatible` is empty; verified): DSM itself doesn't treat it as a problem, so it isn't an attention item; `list_disks` and `get_disk` show it as `not on Synology's compatibility list`, and the overview shows it on the disk's line. On vdsm the Storage pools, Disks and SSD caches sections read `none`.

---

### 2. `list_storage_pools`

**Permission tier:** READ

**DSM API:** `load_info` (`storagePools`, `detected_pools`, `missing_pools`); `Storage.CGI.Pool get_setting {pool_path}` per pool, with the pool's `space_path` (`/dev/vg1`; `Spare list`'s `protect_spaces` and `env.data_scrubbing.sche_pool_order` use the same form), not its `pool_path` field (`reuse_1`); `Storage.CGI.Spare list`, `potential_spares_dump`; `Spare.Conf get`

**Tool description (LLM-facing):**
> List the NAS's storage pools: RAID type, member disks and their state, size and use, status,
> any repair, rebuild or expansion in progress with its percentage, and data scrubbing (state,
> last and next run). Also lists hot spares (which pools each protects), disks that could become
> spares, whether a spare replaces a failed disk automatically, and pools that are missing or
> detected but not assembled. Needs a DSM administrator account.

**Parameters:** None.

A NAS without pools (vdsm, or a model with only `basic` volumes) gets `No storage pools. Volumes on this NAS aren't in a storage pool (see get_volume).`

---

### 3. `list_volumes`

**Permission tier:** READ

**DSM API:** `load_info` (`volumes`)

**Tool description (LLM-facing):**
> List the NAS's volumes: path (/volume1), file system, size, used and free space with the
> percentage used, status and what DSM says about it (for example almost full), the storage pool
> it's on, encryption and lock state (DSM 7.2.2 and later), and data scrubbing state. Needs a DSM
> administrator account.
> For a volume's space by category (shared folders, snapshots, iSCSI LUNs), TRIM and
> deduplication, use get_volume.

**Parameters:** None.

---

### 4. `get_volume`

**Permission tier:** READ

**DSM API:** `load_info` (to resolve the volume); `Storage.CGI.Volume get_space_usage {volume_path}`, `ssd_trim_get {space_path}`; `BtrfsDedupe get_volume_info {vol_paths}`; `Storage get_space_reclaim_status`; for an attached SSD cache, `load_info`'s `ssdCaches` entry whose `mountSpaceId` is the volume's ID, plus `Flashcache shared_cache_config_get {shared_cache}` for a shared cache

**Tool description (LLM-facing):**
> Get everything about one volume: what list_volumes shows plus its space by category (shared
> folders, snapshots, iSCSI LUNs, other), SSD TRIM support and schedule, Btrfs deduplication
> (supported, last run and savings), space reclamation state, the SSD cache attached to it, and
> every other field DSM reports. Needs a DSM administrator account.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `volume` | str | (required) | The volume's path (`/volume1`) or ID (`volume_1`), as list_volumes shows it. |

Space by category is DSM's own usage report (`get_space_usage`), which exists only when "space usage details" is enabled on the volume (`status: enabled`; verified enabled on the real NAS). When it's `disabled`, the reply says so and how to enable it in Storage Manager; enabling it is a phase 2 tool.

---

### 5. `list_disks`

**Permission tier:** READ

**DSM API:** `load_info` (`disks`)

**Tool description (LLM-facing):**
> List the NAS's disks: bay, model, vendor, serial number, firmware, size, type (HDD or SSD,
> SATA, SAS, M.2), temperature, status, S.M.A.R.T. status, bad-sector count, remaining life for
> SSDs, and what each is used for (which pool, hot spare, SSD cache, or not in use). Shows a
> running S.M.A.R.T. or other disk test with its progress. Needs a DSM administrator account.
> For one disk's S.M.A.R.T. attributes, test history and health trend, use get_disk.

**Parameters:** None.

On vdsm: `No disks reported. Virtual DSM has no physical disks.` when `env.model_name` is `VirtualDSM`, else `No disks reported.`

---

### 6. `get_disk`

**Permission tier:** READ

**DSM API:** `load_info` (to resolve the disk); `Storage.CGI.Smart get_health_info {device}`, `get_smart_info {device}`, `smart_warning_get {device}`; `Core.Storage.Disk get_smart_test_log {device}`, `disk_test_log_get {device, offset: 0, limit, sort_by: "time", sort_direction: "DESC"}`, `get_performance_test_status {device}`, `wcache_get {device}`, `disk_health_prediction_log_get`

**Tool description (LLM-facing):**
> Get one disk's full health: everything list_disks shows, every S.M.A.R.T. attribute (current,
> worst, threshold, raw value, status), the last quick and extended S.M.A.R.T. test results, recent
> test history, the 12-month trend of bad sectors, retries and SSD remaining life, any S.M.A.R.T.
> warnings, and its write-cache setting. Needs a DSM administrator account.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `disk` | str | (required) | The disk's ID (`sda`), device (`/dev/sda`) or bay name (`Drive 1`), as list_disks shows it. |
| `history_limit` | int | 10 | How many recent test-history entries to show (1–100). |

`Smart get_smart_info` and `Core.Storage.Disk get_smart_info` return the same attribute table (verified); the tool uses the first. The health trend is `get_health_info.healthInfo.history.incremental`: twelve monthly points per metric (`unc` bad sectors, `idnf`, `retry`, `remainLife`), a point with one element meaning no reading for that month (verified: the SSD's `remainLife` runs 90 → 67 over the year). An HDD's `remainLife` is eleven one-element points and a last point of `[11, -1]`, and the SSD's `unc` ends the same way: `-1` means the drive doesn't report that metric, shown as `—` (verified).

---

### 7. `list_ssd_caches`

**Permission tier:** READ

**DSM API:** `load_info` (`ssdCaches`, `sharedCaches`); `Flashcache shared_cache_config_get {shared_cache}` per shared cache; `Flashcache advisor_history_get`

**Tool description (LLM-facing):**
> List the NAS's SSD caches: which volume each speeds up, read-only or read-write, RAID type and
> SSDs, size, read and write hit rates, status, whether metadata is pinned, and the SSD cache
> advisor's recent recommendations (suggested cache size). Needs a DSM administrator account.

**Parameters:** None.

---

### 8. `get_storage_schedules`

**Permission tier:** READ

**DSM API:** `Storage get_schedule_plan`, `get_space_reclaim_schedule`, `get_sche_task_list`; `Smart.Scheduler list`; `BtrfsDedupe get_schedule_plan`

**Tool description (LLM-facing):**
> Get every scheduled storage task: data scrubbing (on or off, interval, which pools, next run),
> S.M.A.R.T. test tasks (quick or extended, which disks, enabled, next run), space reclamation and
> Btrfs deduplication windows, and SSD TRIM, with what's running now. Needs a DSM administrator
> account. Snapshot schedules are per shared folder: use list_snapshots.

**Parameters:** None.

The scrubbing, reclamation and dedupe windows are DSM's 168-character weekly plans (one character per hour; verified 168 characters, all `1` for scrubbing and reclamation on the real NAS, and a mixed dedupe plan on 7.4.1 vdsm). Which day comes first is inferred from Download Station's plan (Sunday) and to be checked against Storage Manager's grid. They're shown as a grid like Download Station's `get_schedule`; its `format_schedule_grid` is private to that module (`modules/downloadstation/helpers.py`) with Download Station's 0/1/2 legend, so the implementation moves it to `core/formatting.py` with the legend as a parameter.

---

### 9. `get_storage_settings`

**Permission tier:** READ

**DSM API:** `Storage get_resync_speed`, `get_fast_repair_config`, `get_auto_repair_config`; `HddMan get`; `Spare.Conf get`; `Smart get_latest_online_drive_db_info` (DSM 7.2.2+); `Core.ExternalDevice.Storage.Setting get`

**Tool description (LLM-facing):**
> Get the NAS's storage settings: RAID resync speed, fast repair, automatic data scrubbing and
> repair, disk health warning thresholds (bad sectors, SSD remaining life, months of life left),
> health reports, automatic hot-spare replacement, the drive compatibility database's version, and
> external-storage settings. Needs a DSM administrator account.

**Parameters:** None.

---

### 10. `list_external_storage`

**Permission tier:** READ

**DSM API:** `Core.ExternalDevice.Storage.USB list {additional: ["all"]}`, `.eSATA list` (with the same `additional`, to verify), `.EUnit list`

**Tool description (LLM-facing):**
> List storage attached to the NAS from outside: USB and eSATA drives (name, size, file system,
> partitions, shared folder) and expansion units (model, status, disks). Needs a DSM administrator
> account.

**Parameters:** None.

**USB, verified** on the real NAS with a FAT32 stick: without `additional` the list holds only `dev_id` (`sdu`) and `dev_title` (`USB Disk 1`); with `additional: ["all"]` it adds `dev_type` (`usbDisk`), `producer`, `product`, `status`, `total_size_mb`, `formatable`, `progress` and `partitions`, each with `name_id` (`sdu1`), `partition_title`, `filesystem` (`FAT32`), `dev_fstype` (`vfat`), `share_name` (the shared folder DSM created for it; `share19` in the scrubbed fixture), `status`, `total_size_mb` and `used_size_mb`. Sizes are in MB, inferred to be binary MiB (29,604 MiB is 31.0 GB, which fits a stick sold as 32 GB) and are shown with the shared size formatter. The reply carries no serial number. `Core.System info`'s `usb_dev` (the System module's call, not in these fixtures) showed the same stick at hardware level during the capture, with its vendor and product IDs; an earlier stick appeared in neither, and turned out to be dead (it didn't work in a laptop either).

**eSATA and expansion units:** empty on every capture; `eSATA list` is assumed to take the same `additional` (same API family; to verify), and expansion units are shown by their fields' names until a capture has one (open item 1).

---

### 11. `list_snapshots`

**Permission tier:** READ

**DSM API:** `Core.Share list {additional: ["vol_path", "enable_snapshot", "snapshot_count"]}` (the per-share count is `snapshot_info.snapshot_count`); `Core.Share.Snapshot list` v2 `{name, additional: ["desc", "lock", "worm_lock", "schedule_snapshot"], offset, limit}`; `get_schedule {name}`

**Tool description (LLM-facing):**
> List a shared folder's snapshots (time taken, description, locked, immutable on DSM 7.2.2 and
> later, scheduled or manual) and its snapshot schedule. Without a shared folder, lists every shared
> folder's snapshot count and whether a schedule is on. Needs a DSM administrator account and the
> Snapshot Replication package.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `share` | str \| None | None | The shared folder's name (`media`, not a path). Leave empty for a count per shared folder. |
| `offset` | int | 0 | Skip this many snapshots (newest first). |
| `limit` | int | 50 | Show at most this many snapshots (1–500). |

Snapshot times come as DSM's names (`GMT+00-2026.10.28-15.04.00`), which carry their offset; they're shown as UTC instants.

Immutability: the captures carry only `worm_lock: false` (7.2.2+; 7.1 sends no `worm_lock` at all), and no immutable snapshot has been captured, so whether DSM also sends when the lock ends (a third-party client documents `worm_lock_begin`, `worm_lock_end` and `worm_lock_day`) is to verify. Until then the tool shows `worm_lock` and any such field by name.

---

## Phase 2: reversible WRITE tools (planned)

Each gets a capture on vdsm (where the object exists there) before its spec section is written; most act on disks or pools, which vdsm doesn't have, so their writes are verified on the real NAS only with the maintainer's go-ahead, one at a time, and only where reversible.

| Area | Methods | Notes |
|---|---|---|
| Data scrubbing | `Pool data_scrubbing`, `pause_data_scrubbing`, `cancel_data_scrubbing`; `Volume` same; `Storage set_data_scrubbing_schedule`, `set_data_scrubbing_schedule_status`, `set_schedule_plan` | Start, pause, cancel; edit the schedule |
| S.M.A.R.T. tests | `Core.Storage.Disk do_smart_test`; `Smart.Scheduler set`, `run`, `change_state`, `delete`; `Smart smart_warning_set`, `update_smartctl_db` | A test runs for hours on a large HDD; handed off with a handle like File Station's tasks |
| Settings | `Storage set_resync_speed`, `set_fast_repair_config`, `set_auto_repair_config`; `HddMan set`; `Spare.Conf set`; `ExternalDevice.Storage.Setting set` | |
| Hot spares | `Spare add`, `edit`, `delete` | Reversible: no data on a spare |
| Disks | `Core.Storage.Disk wcache_set`, `set_disk_led_status`, `do_performance_test`, `stop_performance_test` | |
| Volumes | `Volume ssd_trim_save`, `set_setting`, `enable_space_usage`, `disable_space_usage`, `defrag`, `cancel_defrag`; `Storage set_space_reclaim_schedule`, `delay_space_reclaim` | |
| Deduplication | `BtrfsDedupe manual_dedupe`, `stop`, `set_schedule_plan`, `set_volume_schedule_config` | Only where supported (121 elsewhere) |
| SSD cache | `Flashcache shared_cache_config_set`, `advisor_start`, `advisor_stop` | Creating or removing a cache changes the layout: excluded |
| External | `Core.ExternalDevice.Storage.USB eject`, `eSATA eject` | `format` erases the device: excluded |
| Snapshots | `Core.Share.Snapshot create`, `set` (description, lock), `delete`, `set_schedule` | `delete` is destructive-annotated, confirm first; a locked or immutable snapshot can't be deleted (to verify) |

---

## Remaining Open Items

1. **eSATA devices and expansion units.** The USB shape is captured (a stick on the real NAS, 2026-10-28); no capture has an eSATA device or an expansion unit, so those are shown by their fields' names until one does.
2. **Snapshot Replication on vdsm.** The golden images don't include it; the captures installed it at runtime (`synopkg install_from_server ReplicationService`, ~seconds). Either bake it in (a bake change: its own maintenance PR, per CLAUDE.md) or install it in a test fixture. Recommendation: bake it, with the next bake change.
3. **`SYNO.Storage.CGI.VolumeReduction`** (DSM 7.4.1 only): methods `get_auto_enable_reduction_for_new`, `list_reduction_share` answer 120 without parameters. Capture with parameters before phase 1's implementation; if it reports compression or dedupe savings, `get_volume` shows it on 7.4.1.
4. **Which fields list tools leave out.** The rule proposed above (UI-only fields only in `get_*`) needs QA's view against the "show every field" rule.
5. **Payload.** Eleven tools add to the tool payload of a connection that enables the module. Measure with `scripts/measure_tools.py` at implementation; if the budget is tight, `list_external_storage` could fold into `get_storage_overview`.
6. **Error codes 117 and 48640.** 48640 is documented only from the unknown-volume capture; 117 from unknown IDs and from missing or wrong parameters (Unknown IDs and Feature not supported, above). Add both to the DSM API notes doc (#270; not written yet) with this module.
7. **Epoch times during daylight saving time.** One capture shows `get_sche_task_list`'s `next_run_time` an hour later than the scheduler's local time while the NAS is on daylight time. Re-capture after DST ends (2026-11-01 in the US): if the two then agree, DSM uses the standard offset and the tools can correct for it; until then epochs are shown as approximate.
