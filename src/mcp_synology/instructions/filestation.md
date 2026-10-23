<!-- File Station's instructions: sent after server.md when the filestation module is enabled,
at either permission (#169). Guidance for the WRITE tools is in filestation-write.md (#318).
Each tool's own description says when to use it and which related tool to use instead
(#247); this file carries only what no single tool does (#377). -->
FILE STATION (the files and folders in the NAS's shared folders):

PATH FORMAT:
All file paths start with a shared folder name: /video/..., /music/..., etc.
Call list_shares first to discover available shared folders.

DOWNLOADING:
When the user says "download" without specifying a local path, use the default
download directory if configured, otherwise ask.

RECYCLE BIN:
A share's recycle bin is the #recycle folder at its top level; list_recycle_bin lists it.
