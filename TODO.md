# Bugs
- [x] Deleting images in the fetch card art workflow using the clear card images button clears the images but changes the command preview to say "waiting for server". Toggling an option on and off brings the command preview back.
- [x] The extra settings under the decklist format dropdown used to be in a collapsable region defaulting to collapsed but are now part of the main UI. Move them back to the collapsed section.
- [x] The decklist source and picker are in 2 columns but it also didn't used to be like this. The decklist picker should be under the decklist source.

# Features
- [x] The macos build workflow double zips the app. main zip -> inside zip -> SCM Workbench.app. Instead of fixing the zip situation let's implement the proper solution by making the file the macos standard installer method where a UI box opens and prompts you to drag the app to the applications folder and you physically drag the app icon over the applications icon (a `.dmg` file). Windows stays as the "portable" solution.
