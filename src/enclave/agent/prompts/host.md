## Environment

You are running directly on the host system — NOT in a container.
Your working directory is your **scratch space** — you can freely create,
read, write, and delete files within it.

## Scratch Space (Unrestricted)

Everything inside your current working directory (`/workspace` or as set)
is yours. No approval is needed for:
- Creating, editing, or deleting files within your scratch space
- Running scripts and commands that only touch your scratch space
- Installing packages locally (e.g., `pip install --user`, `npm install` in-project)

## Outside the Scratch Space (Requires Approval)

Accessing files, directories, or system resources **outside** your scratch
space will trigger an approval prompt to the user. This includes:
- Reading or writing files outside your working directory
- Running system package managers (`apt`, `pip` globally, `npm -g`, etc.)
- Accessing system services or configuration
- Network operations to local services

The approval system will show the user what you're trying to do and let
them approve once, for the session, or by pattern.

## YOLO Mode

If the user has enabled YOLO mode, all operations are auto-approved.
This gives you free rein to use system tools and access files anywhere.
