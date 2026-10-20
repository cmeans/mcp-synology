<!-- File Station's guidance for its WRITE tools: sent after filestation.md when the
filestation module has permission: write or admin (#318). -->
UPLOADING:
- upload_file: Upload a local file to the NAS (local path + NAS destination folder)
It doesn't overwrite an existing file by default — use overwrite=true when intended.

MOVING AND ORGANIZING FILES:
When a user asks to move or organize files:
1. Use search_files to find matching files. Use exclude_pattern to filter out
   unwanted file types (e.g., exclude_pattern="*.torrent" when moving media).
2. Present the results with a count and confirm with the user before proceeding.
3. Use move_files or copy_files with the confirmed paths.
Always search first and confirm before destructive operations.

RESTORING DELETED FILES:
restore_from_recycle_bin recovers files that list_recycle_bin shows. Deleting anything
inside a recycle bin is permanent.

LONG OPERATIONS:
A large delete, copy, move or restore can outlast one reply. The tool then hands it off:
the NAS keeps working, and the reply gives a handle. Tell the user it's still running,
and include the progress. Don't poll in a loop; check with file_task when the user asks,
or before doing anything that depends on the result, such as reusing the destination or
reporting space freed. Never start the same operation again while it's running, and
don't stop and restart it because progress looks slow: a delete counts finished items, and
one large file can take many minutes on a busy NAS. Stop it (file_task action="stop") only when the
user wants it canceled, and say the result may be partial.
