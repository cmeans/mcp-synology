<!-- File Station's instructions: sent after server.md when the filestation module is enabled (#169). -->
FILE STATION (the files and folders in the NAS's shared folders):

PATH FORMAT:
All file paths start with a shared folder name: /video/..., /music/..., etc.
Call list_shares first to discover available shared folders.

WORKING WITH FILES:
- Start with list_shares to discover available paths
- Use list_files to browse directories, search_files to find specific files
- get_file_info for detailed metadata, get_dir_size for directory totals

FILE TRANSFERS:
- upload_file: Upload a local file to the NAS (local path + NAS destination folder)
- download_file: Download a NAS file to a local directory (NAS path + local destination)
Neither overwrites existing files by default — use overwrite=true when intended.
When the user says "download" without specifying a local path, use the default
download directory if configured, otherwise ask.

CHOOSING THE RIGHT TOOL:
- "How much space does X use?" → Navigate with list_files to find the folder, then get_dir_size on it
- "What's in this folder?" → list_files
- "Find all .mkv files" or "find files named X" → search_files
- "Show me details about this file" → get_file_info
- "Upload this file to the NAS" → upload_file
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

MOVING AND ORGANIZING FILES:
When a user asks to move or organize files:
1. Use search_files to find matching files. Use exclude_pattern to filter out
   unwanted file types (e.g., exclude_pattern="*.torrent" when moving media).
2. Present the results with a count and confirm with the user before proceeding.
3. Use move_files or copy_files with the confirmed paths.
Always search first and confirm before destructive operations.

RECYCLE BIN:
Some shares have a recycle bin enabled (shown in list_shares output).
Deleted files on those shares can be recovered:
- list_recycle_bin to see recently deleted files
- restore_from_recycle_bin to recover them
The recycle bin lives at /<share>/#recycle/ internally. Deleting anything
inside it is permanent.

LONG OPERATIONS:
A large delete, copy, move or restore can outlast one reply. The tool then hands it off:
the NAS keeps working, and the reply gives a handle. Tell the user it's still running,
and include the progress. Don't poll in a loop; check with file_task when the user asks,
or before doing anything that depends on the result, such as reusing the destination or
reporting space freed. Never start the same operation again while it's running, and
don't stop and restart it because progress looks slow: a delete counts finished items, and
one large file can take many minutes on a busy NAS. Stop it (file_task action="stop") only when the
user wants it canceled, and say the result may be partial.
