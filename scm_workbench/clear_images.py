#!/usr/bin/env python3
### Workbench: clears the card image folders (game/front/ + game/double_sided/)
###
### The Workbench's own take on SCM's clean_up.py: it deletes exactly the
### same things, but the folder README/EMPTY placeholders are kept — SCM's
### script only knows the old EMPTY.md name, so running it directly would
### also wipe the README.md placeholders the current repo versions ship.
### The card back folder (game/back/) is left untouched, same as upstream.
import os
import shutil

# placeholder files the upstream repos keep in these folders — never delete
KEPT = {"README.md", "EMPTY.md"}


def delete_files():
    root_path = "game"
    image_folders = ["front", "double_sided"]
    i = 0
    kept = 0

    for folder_name in image_folders:
        working_path = os.path.join(root_path, folder_name)
        if not os.path.isdir(working_path):
            continue

        for item in os.listdir(working_path):
            full_path = os.path.join(working_path, item)

            if os.path.basename(full_path) in KEPT:
                kept += 1
                continue

            if os.path.isfile(full_path):
                os.remove(full_path)
                print(f"Deleted file {full_path}")
                i += 1
            elif os.path.isdir(full_path):
                shutil.rmtree(full_path)
                print(f"Deleted directory {full_path}")
                i += 1

    print(f"Deleted {i} item{'s' if i != 1 else ''}"
          + (f" (kept {kept} folder placeholder{'s' if kept != 1 else ''})" if kept else ""))


if __name__ == "__main__":
    delete_files()
