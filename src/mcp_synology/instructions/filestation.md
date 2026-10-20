<!-- File Station's instructions: sent after server.md when the filestation module is enabled,
at either permission (#169). Guidance for the WRITE tools is in filestation-write.md (#318). -->
FILE STATION (the files and folders in the NAS's shared folders):

PATH FORMAT:
All file paths start with a shared folder name: /video/..., /music/..., etc.
Call list_shares first to discover available shared folders.

WORKING WITH FILES:
- Start with list_shares to discover available paths
- Use list_files to browse directories, search_files to find specific files
- get_file_info for detailed metadata, get_dir_size for directory totals

DOWNLOADING:
- download_file: Download a NAS file to a local directory (NAS path + local destination)
It doesn't overwrite an existing file by default — use overwrite=true when intended.
When the user says "download" without specifying a local path, use the default
download directory if configured, otherwise ask.

CHOOSING THE RIGHT TOOL:
- "How much space does X use?" → Navigate with list_files to find the folder, then get_dir_size on it
- "What's in this folder?" → list_files
- "Find all .mkv files" or "find files named X" → search_files
- "Show me details about this file" → get_file_info
- "Download this file from the NAS" → download_file

BROWSING vs SEARCHING:
- list_files shows one directory level. Its pattern parameter supports glob filtering (e.g., "*.mkv")
- search_files searches recursively. Its pattern is a keyword/substring match (NOT glob).
  Use the extension parameter for file type filtering (e.g., extension="mkv")
- search_files is slower — prefer list_files + get_dir_size when you know the path

DIRECTORY SIZE:
get_dir_size calculates the total size of everything under a path. When asked about
disk usage for a specific folder or show, navigate to the right path with list_files
first, then call get_dir_size on that path. Do NOT use search_files to estimate sizes.

RECYCLE BIN:
Some shares have a recycle bin enabled (shown in list_shares output).
list_recycle_bin shows the files recently deleted on those shares.
The recycle bin lives at /<share>/#recycle/ internally.
