<!-- Template variables available for use in this file and in config custom_instructions:
  {display_name}  — alias if set, otherwise instance_id (e.g., "HomeNAS")
  {instance_id}   — derived from host or set explicitly (e.g., "192-168-200-52")
  {host}          — NAS hostname or IP (e.g., "192.168.200.52")
  {port}          — connection port (e.g., "5000")
  {home_dir}      — local user home directory (e.g., "/home/chris")
  {platform}      — local OS: "Linux", "macOS", or "Windows"

The server sends this file on every connection, then each enabled module's own file
(filestation.md when File Station is enabled, then filestation-write.md when its WRITE tools
are), then the generated sections. local.md opens the LOCAL PATHS section when a tool that
reads or writes local files is enabled (#169).
-->
SERVER IDENTITY: This is the '{display_name}' NAS connection ({host}:{port}).
Only use these tools when the user refers to '{display_name}'.
Do NOT fall back to a different Synology server if an operation fails — ask the user instead.

You are connected to a Synology NAS through mcp-synology.

FILE SIZES:
Sizes are binary (1 KiB = 1024 bytes). Size parameters accept "500MiB", "2GiB",
"1.5TiB"; KB/MB/GB/TB mean the same binary units.

TIMES:
Times ending in UTC are exact: give them in the user's own time zone. Times with no zone are
local clock times; show them as they are.
